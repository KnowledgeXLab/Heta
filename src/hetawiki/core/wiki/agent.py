from __future__ import annotations

import json
import logging

from json_repair import repair_json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from openai import OpenAI

from common.config import load_config
from hetawiki.utils.text import parse_frontmatter, slugify

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Read a wiki file from the working copy. Valid paths: index.md or pages/*.md.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_page",
            "description": "Create a new wiki page. Valid paths: pages/*.md only. Fails if the file already exists; use edit_page to modify existing pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_page",
            "description": "Edit a wiki file by replacing an exact string. Valid paths: pages/*.md or index.md. old_str must match exactly one location in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_page",
            "description": "Delete a wiki page. Valid paths: pages/*.md only. Cannot delete index.md or log.md.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_log",
            "description": "Append a message to the wiki log. Timestamp and formatting are added automatically.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
]


def _get_client() -> tuple[OpenAI, str, int]:
    cfg = load_config("hetawiki")
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg:
        raise RuntimeError("Missing `hetawiki.llm` configuration in config.yaml")

    base_url = str(llm_cfg["base_url"])
    if not base_url.endswith("/"):
        base_url += "/"

    client = OpenAI(api_key=llm_cfg["api_key"], base_url=base_url, timeout=int(llm_cfg.get("timeout", 120)))
    return client, str(llm_cfg["model"]), int(llm_cfg.get("max_retries", 2))


def _validate_pages_only(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if normalized.startswith("pages/") and normalized.endswith(".md"):
        return normalized
    raise ValueError(f"path must be pages/*.md, got: {path!r}")


def _validate_pages_or_index(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if normalized == "index.md" or (normalized.startswith("pages/") and normalized.endswith(".md")):
        return normalized
    raise ValueError(f"path must be index.md or pages/*.md, got: {path!r}")


def _resolve_safe(root_dir: Path, normalized: str) -> Path:
    candidate = (root_dir / normalized).resolve()
    if root_dir.resolve() not in candidate.parents and candidate != root_dir.resolve():
        raise ValueError(f"path escapes working copy: {normalized!r}")
    return candidate


def read_page(root_dir: Path, path: str) -> str:
    try:
        normalized = _validate_pages_or_index(path)
        full = _resolve_safe(root_dir, normalized)
        if not full.exists():
            return f"error: {normalized} does not exist"
        return full.read_text(encoding="utf-8")
    except Exception as exc:
        return f"error: {exc}"


def create_page(root_dir: Path, path: str, content: str, written_paths: set[str]) -> str:
    try:
        _validate_pages_only(path)  # validate format only
        fm = parse_frontmatter(content)
        title = fm.get("title", "").strip()
        # Always derive canonical path from title; never trust the LLM's guess
        normalized = f"pages/{slugify(title)}.md" if title else _validate_pages_only(path)
        full = _resolve_safe(root_dir, normalized)
        if full.exists():
            return f"error: {normalized} already exists; use edit_page to modify it"
        full.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(full.parent)) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        temp_path.replace(full)
        written_paths.add(normalized)
        return f"ok: created {normalized} ({len(content)} chars)"
    except Exception as exc:
        return f"error: {exc}"


def edit_page(root_dir: Path, path: str, old_str: str, new_str: str, written_paths: set[str]) -> str:
    try:
        normalized = _validate_pages_or_index(path)
        full = _resolve_safe(root_dir, normalized)
        if not full.exists():
            return f"error: {normalized} does not exist; use create_page to create it"
        content = full.read_text(encoding="utf-8")
        count = content.count(old_str)
        if count == 0:
            return "error: old_str not found in file"
        if count > 1:
            return f"error: old_str matches {count} locations; add more context to make it unique"
        new_content = content.replace(old_str, new_str, 1)
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(full.parent)) as tmp:
            tmp.write(new_content)
            temp_path = Path(tmp.name)
        temp_path.replace(full)
        written_paths.add(normalized)
        return f"ok: edited {normalized}"
    except Exception as exc:
        return f"error: {exc}"


def delete_page(root_dir: Path, path: str, written_paths: set[str]) -> str:
    try:
        normalized = _validate_pages_only(path)
        full = _resolve_safe(root_dir, normalized)
        if not full.exists():
            return f"error: {normalized} does not exist"
        full.unlink()
        written_paths.discard(normalized)
        return f"ok: deleted {normalized}"
    except Exception as exc:
        return f"error: {exc}"


def append_log(root_dir: Path, message: str) -> str:
    try:
        log_path = root_dir / "log.md"
        if not log_path.exists():
            return "error: log.md does not exist in working copy"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"- [{timestamp}] {message}\n")
        return "ok: appended to log.md"
    except Exception as exc:
        return f"error: {exc}"


def _execute_tools(
    root_dir: Path,
    tool_calls: list[Any],
    written_paths: set[str],
    read_paths: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        name = tool_call.function.name
        try:
            arguments = json.loads(repair_json(tool_call.function.arguments or "{}"))
        except Exception as exc:
            output = f"error: invalid tool arguments: {exc}"
        else:
            if name == "read_page":
                output = read_page(root_dir, **arguments)
                if not output.startswith("error:"):
                    normalized = arguments.get("path", "").replace("\\", "/").strip("/")
                    read_paths.add(normalized)
            elif name == "create_page":
                output = create_page(root_dir, written_paths=written_paths, **arguments)
            elif name == "edit_page":
                output = edit_page(root_dir, written_paths=written_paths, **arguments)
            elif name == "delete_page":
                output = delete_page(root_dir, written_paths=written_paths, **arguments)
            elif name == "append_log":
                output = append_log(root_dir, **arguments)
            else:
                output = f"error: unknown tool {name}"

        results.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }
        )
    return results


@dataclass
class AgentHarness:
    task_id: str
    max_steps: int = 15
    max_seconds: int = 300
    steps: int = 0
    tokens: int = 0
    start: float = field(default_factory=time.time)
    events: list[str] = field(default_factory=list)

    def should_continue(self) -> bool:
        if self.steps >= self.max_steps:
            self.log("aborted: max steps reached")
            return False
        if time.time() - self.start > self.max_seconds:
            self.log("aborted: timeout")
            return False
        return True

    def record(self, tool_names: str, usage: Any) -> None:
        self.steps += 1
        self._add_usage(usage)
        self.log(f"step {self.steps} | {tool_names}")

    def record_completion(self, usage: Any) -> None:
        self._add_usage(usage)

    def _add_usage(self, usage: Any) -> None:
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        self.tokens += prompt_tokens + completion_tokens
        self.log(f"usage | {prompt_tokens}t in | {completion_tokens}t out")

    def finish(self, summary: str) -> dict[str, Any]:
        elapsed = int(time.time() - self.start)
        self.log(f"done | {summary} | {self.tokens} tokens | {elapsed}s")
        return {
            "steps": self.steps,
            "tokens": self.tokens,
            "elapsed_s": elapsed,
            "events": self.events,
        }

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.events.append(entry)
        logger.info("[agent:%s] %s", self.task_id, message)


def run_agent(
    *,
    task_id: str,
    system: str,
    initial_message: str,
    root_dir: Path,
    max_steps: int = 15,
    max_seconds: int = 300,
    temperature: float = 0.2,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    client, model, max_retries = _get_client()
    harness = AgentHarness(task_id=task_id, max_steps=max_steps, max_seconds=max_seconds)
    messages: list[dict[str, Any]] = [{"role": "user", "content": initial_message}]
    written_paths: set[str] = set()
    read_paths: set[str] = set()
    active_tools = tools if tools is not None else TOOLS

    while harness.should_continue():
        last_error: str | None = None
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, *messages],
                    tools=active_tools,
                    tool_choice="auto",
                    temperature=temperature,
                    extra_body={"enable_thinking": False},
                )
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning("agent request failed (attempt %s/%s): %s", attempt + 1, max_retries + 1, exc)
        if response is None:
            raise RuntimeError(f"agent failed after {max_retries + 1} attempts: {last_error}")

        message = response.choices[0].message
        tool_calls = list(message.tool_calls or [])

        # ── Log LLM output ──────────────────────────────────────────────────
        if message.content:
            logger.debug("[agent:%s] ← assistant: %s", task_id, message.content[:500])
        for tc in tool_calls:
            logger.info("[agent:%s] ← tool_call: %s(%s)",
                        task_id, tc.function.name,
                        tc.function.arguments[:200] if tc.function.arguments else "")

        if not tool_calls:
            final_text = message.content or "completed"
            harness.record_completion(response.usage)
            logger.info("[agent:%s] ← final: %s", task_id, final_text[:300])
            return {
                "final_response": final_text,
                "written_paths": sorted(written_paths),
                "read_paths": sorted(read_paths),
                "usage": harness.finish("completed"),
            }

        assistant_message: dict[str, Any] = {"role": "assistant"}
        if message.content:
            assistant_message["content"] = message.content
        assistant_message["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in tool_calls
        ]
        messages.append(assistant_message)

        tool_results = _execute_tools(root_dir, tool_calls, written_paths, read_paths)

        # ── Log tool results ────────────────────────────────────────────────
        for tr in tool_results:
            logger.info("[agent:%s] → tool_result: %s",
                        task_id, str(tr.get("content", ""))[:300])

        messages.extend(tool_results)
        tool_names = ", ".join(tool_call.function.name for tool_call in tool_calls)
        harness.record(tool_names, response.usage)

    # Limit reached — make one final call without tools to get a closing response
    logger.warning("agent reached limit (%d steps / %ds); requesting final response", harness.steps, int(time.time() - harness.start))
    messages.append({
        "role": "user",
        "content": "You have reached the step/time limit. Do NOT call any more tools. Write your final response now summarising what you have done.",
    })
    try:
        final = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, *messages],
            temperature=temperature,
            extra_body={"enable_thinking": False},
        )
        final_text = final.choices[0].message.content or "completed (limit reached)"
        harness.record_completion(final.usage)
    except Exception as exc:
        logger.error("final-response call failed: %s", exc)
        final_text = "completed (limit reached)"

    return {
        "final_response": final_text,
        "written_paths": sorted(written_paths),
        "read_paths": sorted(read_paths),
        "usage": harness.finish("stopped at limit"),
    }
