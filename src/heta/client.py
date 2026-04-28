"""HTTP orchestration helpers for the Heta CLI."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from common.config import load_config
from common.llm_client import create_use_llm

SUPPORTED_INSERT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".html",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp", ".ico",
    ".txt", ".text", ".md", ".markdown",
    ".csv", ".xls", ".xlsx", ".ods",
}

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_AGENT_ID = "agent"
RELATED_MEMORY_SCORE = 0.65


class HetaCliError(RuntimeError):
    """Raised when CLI orchestration cannot complete."""


@dataclass(frozen=True)
class CliSettings:
    """Runtime settings shared by CLI commands."""

    base_url: str
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 2.0


def load_cli_settings(base_url: str | None = None) -> CliSettings:
    """Load CLI settings from flags, environment, and config defaults."""
    resolved_base_url = (base_url or os.getenv("HETA_BASE_URL") or _default_base_url()).rstrip("/")
    return CliSettings(base_url=resolved_base_url)


def _default_base_url() -> str:
    try:
        cfg = load_config("heta")
    except FileNotFoundError:
        cfg = {}
    fastapi_cfg = cfg.get("fastapi", {})
    host = fastapi_cfg.get("host", "127.0.0.1")
    port = fastapi_cfg.get("port", 8000)
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def load_judge_config() -> dict[str, Any]:
    """Load the CLI judge LLM config, falling back to HetaDB's LLM config."""
    try:
        cfg = load_config()
    except FileNotFoundError as exc:
        raise HetaCliError("config.yaml not found. Create one from config.example.yaml.") from exc

    judge = cfg.get("heta_cli", {}).get("judge") or cfg.get("hetadb", {}).get("llm") or {}
    missing = [key for key in ("api_key", "base_url", "model") if not judge.get(key)]
    if missing:
        raise HetaCliError(
            "Missing Heta CLI judge config: "
            + ", ".join(f"heta_cli.judge.{key}" for key in missing)
        )
    return judge


def collect_insert_files(targets: tuple[str, ...]) -> list[Path]:
    """Resolve insert targets into a sorted, de-duplicated list of supported files."""
    resolved_targets = targets or (".",)
    collected: list[Path] = []

    for target in resolved_targets:
        matches = _expand_target(target)
        if not matches:
            raise HetaCliError(f"No files matched: {target}")
        for path in matches:
            path = path.expanduser().resolve()
            if not path.exists():
                raise HetaCliError(f"Path not found: {path}")
            if path.is_file():
                collected.append(path)
            elif path.is_dir():
                collected.extend(sorted(p.resolve() for p in path.iterdir() if p.is_file()))
            else:
                raise HetaCliError(f"Unsupported path type: {path}")

    files = _dedupe_sorted(collected)
    if not files:
        raise HetaCliError("No files found.")

    unsupported = [p for p in files if p.suffix.lower() not in SUPPORTED_INSERT_EXTENSIONS]
    if unsupported:
        supported = " ".join(sorted(SUPPORTED_INSERT_EXTENSIONS))
        lines = ["Unsupported file type:"]
        lines.extend(f"  - {p}" for p in unsupported)
        lines.extend(["", f"Supported types: {supported}", "", "Nothing was inserted."])
        raise HetaCliError("\n".join(lines))

    return files


def _expand_target(target: str) -> list[Path]:
    if any(char in target for char in "*?[]"):
        return [Path(match) for match in glob(target)]
    return [Path(target)]


def _dedupe_sorted(paths: list[Path]) -> list[Path]:
    return sorted(set(paths), key=lambda p: str(p))


def build_dataset_name(kb_name: str) -> str:
    """Create a safe dataset name for CLI-managed inserts."""
    safe_kb = re.sub(r"[^A-Za-z0-9_]+", "_", kb_name).strip("_") or "kb"
    safe_kb = safe_kb[:24]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"cli_{safe_kb}_{stamp}_{uuid4().hex[:6]}"


def describe_http_error(detail: Any) -> str:
    """Convert API error payloads into a compact string."""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


class HetaClient:
    """Thin HTTP client for CLI orchestration."""

    def __init__(self, settings: CliSettings):
        self.settings = settings
        self.base_url = settings.base_url
        self._http = httpx.Client(base_url=self.base_url, timeout=settings.timeout_seconds)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HetaClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def check_health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/hetadb/files/knowledge-bases")
        return payload.get("data", [])

    def create_knowledge_base(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/hetadb/files/knowledge-bases", json={"name": name})

    def ensure_knowledge_base(self, name: str) -> None:
        existing = {item.get("name") for item in self.list_knowledge_bases()}
        if name not in existing:
            self.create_knowledge_base(name)

    def create_dataset(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/hetadb/files/raw-files/datasets", json={"name": name})

    def upload_file(self, dataset: str, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self._request(
                "POST",
                f"/api/v1/hetadb/files/raw-files/datasets/{dataset}/file",
                files={"file": (path.name, handle, "application/octet-stream")},
            )

    def parse_dataset(self, kb_name: str, dataset: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/hetadb/files/knowledge-bases/{kb_name}/parse",
            json={"datasets": [dataset], "mode": 0},
        )

    def get_processing_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/hetadb/files/processing/tasks/{task_id}")

    def search_memory_vg(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/api/v1/hetamem/vg/search",
            json={
                "query": query,
                "user_id": DEFAULT_AGENT_ID,
                "agent_id": DEFAULT_AGENT_ID,
                "limit": limit,
            },
        )
        results = payload.get("results", [])
        return results if isinstance(results, list) else []

    def query_memory_kb(self, query: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/hetamem/kb/query",
            json={"query": query, "mode": "hybrid", "use_pm": False},
        )

    def query_hetadb(self, query: str, kb_name: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/hetadb/chat",
            json={
                "query": query,
                "kb_id": kb_name,
                "user_id": DEFAULT_AGENT_ID,
                "query_mode": "rerank",
            },
        )

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HetaCliError(
                f"Cannot reach Heta at {self.base_url}. Start it with `heta serve` "
                "or `./scripts/bootstrap.sh`."
            ) from exc

        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                detail: Any = response.text.strip() or f"HTTP {response.status_code}"
            else:
                detail = payload.get("detail") or payload.get("message") or payload
            raise HetaCliError(
                f"{method} {path} failed ({response.status_code}): {describe_http_error(detail)}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise HetaCliError(f"{method} {path} returned non-JSON response") from exc


class AnswerJudge:
    """LLM judge for deciding whether a candidate answer satisfies a question."""

    def __init__(self, config: dict[str, Any]):
        self._use_llm = create_use_llm(
            url=config["base_url"],
            api_key=config["api_key"],
            model=config["model"],
            timeout=int(config.get("timeout", 60)),
            max_retries=int(config.get("max_retries", 3)),
        )

    def accepts(self, question: str, answer: str) -> bool:
        answer = answer.strip()
        if not answer:
            return False

        prompt = _build_judge_prompt(question, answer)
        response = self._use_llm(prompt)
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise HetaCliError(f"Judge returned non-JSON response: {response[:200]}") from exc
        return payload.get("ok") is True


def related_memories(results: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    """Return high-similarity memories suitable for secondary display."""
    return [item for item in results if _score(item) >= RELATED_MEMORY_SCORE][:limit]


def _score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _build_judge_prompt(question: str, answer: str) -> str:
    return f"""You are judging whether a candidate answer fully answers a user question.

Return strict JSON only:
{{"ok": true}}
or
{{"ok": false}}

Rules:
- ok=true only if the answer directly and sufficiently answers the question.
- ok=false if the answer is vague, unrelated, only partially answers, repeats the question, or says it does not know.
- Do not judge whether the answer is factually true beyond the provided text. Only judge answer suitability.

Question:
{question}

Candidate answer:
{answer}
"""
