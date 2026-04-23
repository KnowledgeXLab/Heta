"""Multi-hop reasoning agent.

Uses a ReAct-style agent (qwen_agent) that iteratively queries the knowledge
base, extracts useful information, and decides when enough evidence has been
gathered to produce a final answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from typing import Any, Literal

import torch
from openai import OpenAI
from qwen_agent.agents.fncall_agent import FnCallAgent
from qwen_agent.llm import BaseChatModel
from qwen_agent.llm.schema import ASSISTANT, DEFAULT_SYSTEM_MESSAGE, Message
from qwen_agent.settings import MAX_LLM_CALL_PER_RUN
from qwen_agent.tools.base import BaseTool, register_tool
from qwen_agent.utils.utils import format_as_text_message, merge_generate_cfgs

from hetadb.utils.load_config import get_chat_cfg

logger = logging.getLogger(__name__)

_TRACE_LOCK = threading.Lock()
_TRACE_STORE: dict[str, list[dict[str, Any]]] = {}

# ---------------------------------------------------------------------------
# Prompt templates (do NOT modify)
# ---------------------------------------------------------------------------

TOOL_DESC = (
    "{name_for_model}: Call this tool to interact with the {name_for_human} API. "
    "What is the {name_for_human} API useful for? {description_for_model} Parameters: {parameters} {args_format}"
)
SYSTEM_EXPLORER = """ Find quailty sources and the right information. You have access to the following tools:

{tool_descs}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action

Notice:
- You must take action at every step. When you take action, you must use the tool with the correct format and output the action input.
- You can not say "I'm sorry, but I cannot assist with this request."!!! You must explore.
- You do not need to provide the final answer, but you must explore.
- Action Input should be valid JSON.

Begin!

{query}
"""

STSTEM_CRITIIC_INFORMATION = """You are an information extraction agent. Your task is to analyze the given observation and extract information relevant to the current query. You need to decide if the observation contains useful information for the query. If it does, return a JSON object with a "usefulness" value of true and an "information" field with the relevant details. If not, return a JSON object with a "usefulness" value of false.

**Input:**
- Query: "<Query>"
- Observation: "<Current Observation>"

**Output (JSON):**
{
  "usefulness": true,
  "information": "<Extracted Useful Information> using string format"
}
Or, if the observation does not contain useful information:
{
  "usefulness": false
}
Only respond with valid JSON.

"""

STSTEM_CRITIIC_ANSWER = """You are a query answering agent. Your task is to evaluate whether the accumulated useful information is sufficient to answer the current query. If it is sufficient, return a JSON object with a "judge" value of true and an "answer" field with the answer. If the information is insufficient, return a JSON object with a "judge" value of false.

**Input:**
- Query: "<Query>"
- Accumulated Information: "<Accumulated Useful Information>"


**Output (JSON):**
{
    "judge": true,
    "answer": "<Generated Answer> using string format"
}
Or, if the information is insufficient to answer the query:
{
    "judge": false
}
Only respond with valid JSON.
"""

MAX_SNIPPET_CHARS = 400


# ---------------------------------------------------------------------------
# ReAct explorer agent
# ---------------------------------------------------------------------------

class HAgent(FnCallAgent):
    """ReAct-format agent that calls tools to gather evidence."""

    def __init__(
        self,
        function_list: list[str | dict | BaseTool] | None = None,
        llm: dict | BaseChatModel | None = None,
        system_message: str | None = DEFAULT_SYSTEM_MESSAGE,
        name: str | None = None,
        description: str | None = None,
        files: list[str] | None = None,
        **kwargs,
    ):
        super().__init__(
            function_list=function_list,
            llm=llm,
            system_message=system_message,
            name=name,
            description=description,
            files=files,
            **kwargs,
        )
        base_cfg: dict[str, Any] = getattr(self, "extra_generate_cfg", {})
        if not isinstance(base_cfg, dict):
            base_cfg = {}
        self.extra_generate_cfg = merge_generate_cfgs(
            base_generate_cfg=base_cfg,
            new_generate_cfg={"stop": ["Observation:", "Observation:\n"]},
        )

        if not isinstance(llm, dict):
            raise ValueError("llm must be a dict for HAgent")
        self.client = OpenAI(
            api_key=llm["api_key"],
            base_url=llm["model_server"],
            timeout=llm.get("timeout"),
        )
        self.llm_cfg: dict[str, Any] = llm
        self.momery: list[Any] = []

    def observation_information_extraction(self, query: str, observation: str) -> str | None:
        """Ask the LLM whether *observation* contains info relevant to *query*."""
        user_prompt = f"- Query: {query}\n- Observation: {observation}"
        messages = [
            {"role": "system", "content": STSTEM_CRITIIC_INFORMATION},
            {"role": "user", "content": user_prompt},
        ]
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.llm_cfg["model"],
                    response_format={"type": "json_object"},
                    messages=messages,
                    extra_body={"enable_thinking": False},
                )
                content = response.choices[0].message.content
                logger.debug("Information extraction response: %s", content)
                if "true" in content:
                    try:
                        return json.loads(content)["information"]
                    except Exception:
                        return content
                return None
            except Exception as exc:
                logger.warning(
                    "Information extraction attempt %d failed: %s", attempt + 1, exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                else:
                    raise

    def critic_information(self, query: str, memory: list[str]) -> str | None:
        """Judge whether accumulated *memory* is sufficient to answer *query*."""
        joined = "-".join(memory)
        user_prompt = f"- Query: {query}\n- Accumulated Information: {joined}"
        messages = [
            {"role": "system", "content": STSTEM_CRITIIC_ANSWER},
            {"role": "user", "content": user_prompt},
        ]
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.llm_cfg["model"],
                    response_format={"type": "json_object"},
                    messages=messages,
                    extra_body={"enable_thinking": False},
                )
                content = response.choices[0].message.content
                logger.debug("Critic answer response: %s", content)
                if "true" in content:
                    try:
                        return json.loads(content)["answer"]
                    except Exception:
                        return content
                return None
            except Exception as exc:
                logger.warning(
                    "Critic information attempt %d failed: %s", attempt + 1, exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(min(2 ** attempt, 30))
                else:
                    raise

    def _run(
        self, messages: list[Message], lang: Literal["en", "zh"] = "en", **kwargs,
    ) -> Iterator[list[Message]]:
        text_messages = self._prepend_react_prompt(messages, lang=lang)
        response: str = "Thought: "
        query = self.llm_cfg.get("query", "")
        action_count = self.llm_cfg.get("action_count", MAX_LLM_CALL_PER_RUN)
        num_llm_calls_available = action_count

        while num_llm_calls_available > 0:
            num_llm_calls_available -= 1
            output = []
            for output in self._call_llm(messages=text_messages):
                if output:
                    yield [Message(role=ASSISTANT, content=output[-1].content)]

            if output:
                response += output[-1].content

            has_action, action, action_input, thought = self._detect_tool(
                "\n" + output[-1].content,
            )

            if not has_action:
                if "Final Answer: " in output[-1].content:
                    break
                continue

            # Execute tool and run information extraction + critic
            query = self.llm_cfg.get("query", "")
            observation = self._call_tool(
                action, action_input, messages=messages, **kwargs,
            )
            stage1 = self.observation_information_extraction(query, observation)
            if stage1:
                self.momery.append(stage1 + "\n")
                if len(self.momery) > 1:
                    yield [
                        Message(
                            role=ASSISTANT,
                            content="Memory:\n" + "-".join(self.momery) + '"}',
                        )
                    ]
                else:
                    yield [
                        Message(
                            role=ASSISTANT,
                            content="Memory:\n" + "-" + self.momery[0] + '"}',
                        )
                    ]
                stage2 = self.critic_information(query, self.momery)
                if stage2:
                    response = f"Final Answer: {stage2}"
                    yield [Message(role=ASSISTANT, content=response)]
                    break

            observation = f"\nObservation: {observation}\nThought: "
            response += observation

            if (not text_messages[-1].content.endswith("\nThought: ")) and (
                not thought.startswith("\n")
            ):
                text_messages[-1].content += "\n"
            if action_input.startswith("```"):
                action_input = "\n" + action_input
            text_messages[-1].content += (
                thought
                + f"\nAction: {action}\nAction Input: {action_input}"
                + observation
            )

    def _prepend_react_prompt(
        self, messages: list[Message], lang: Literal["en", "zh"],
    ) -> list[Message]:
        """Build the ReAct system prompt with tool descriptions."""
        tool_descs_list: list[str] = []
        for f in self.function_map.values():
            function = f.function
            name = function.get("name", None)
            name_for_human = function.get("name_for_human", name)
            name_for_model = function.get("name_for_model", name)
            assert name_for_human and name_for_model
            args_format = function.get("args_format", "")
            tool_descs_list.append(
                TOOL_DESC.format(
                    name_for_human=name_for_human,
                    name_for_model=name_for_model,
                    description_for_model=function["description"],
                    parameters=json.dumps(function["parameters"], ensure_ascii=False),
                    args_format=args_format,
                ).rstrip()
            )
        tool_descs = "\n\n".join(tool_descs_list)
        tool_names = ",".join(tool.name for tool in self.function_map.values())
        text_messages = [
            format_as_text_message(m, add_upload_info=True, lang=lang) for m in messages
        ]
        text_messages[-1].content = SYSTEM_EXPLORER.format(
            tool_descs=tool_descs,
            tool_names=tool_names,
            query=text_messages[-1].content,
        )
        return text_messages

    def _detect_tool(self, text: str) -> tuple[bool, str, str, str]:
        """Parse ReAct output for Action / Action Input tokens."""
        special_func_token = "\nAction:"
        special_args_token = "\nAction Input:"
        special_obs_token = "\nObservation:"
        func_name: str | None = None
        func_args: str | None = None
        i = text.rfind(special_func_token)
        j = text.rfind(special_args_token)
        k = text.rfind(special_obs_token)
        if 0 <= i < j:
            if k < j:
                text = text.rstrip() + special_obs_token
            k = text.rfind(special_obs_token)
            func_name = text[i + len(special_func_token) : j].strip()
            func_args = text[j + len(special_args_token) : k].strip()
            text = text[:i]
        return (func_name is not None), func_name or "", func_args or "", text


# ---------------------------------------------------------------------------
# Module-level LLM config (read once at import time)
# ---------------------------------------------------------------------------

# Workaround for a PyTorch path-inspection bug present in some torch versions.
try:
    if hasattr(torch.classes, "__file__") and torch.classes.__file__:
        torch.classes.__path__ = [os.path.join(torch.__path__[0], torch.classes.__file__)]
except Exception:
    pass

_chat_cfg = get_chat_cfg()
if not _chat_cfg:
    raise RuntimeError("multi_hop_qa: LLM config (hetadb.llm) is missing in config.yaml")

llm_cfg: dict[str, Any] = {
    "model": _chat_cfg["model"],
    "api_key": _chat_cfg["api_key"],
    "model_server": _chat_cfg["base_url"],
    "generate_cfg": _chat_cfg.get("generate_cfg", {}),
    "timeout": _chat_cfg.get("timeout"),
}


# ---------------------------------------------------------------------------
# Knowledge-base query tool (registered for qwen_agent)
# ---------------------------------------------------------------------------

@register_tool("knowledge_query", allow_overwrite=True)
class KnowledgeQueryTool(BaseTool):
    """Tool wrapper around ``perform_knowledge_query`` so the agent can pull
    structured evidence from the knowledge base during multi-hop reasoning.
    """

    description = (
        "Accesses the knowledge base via perform_knowledge_query and returns the "
        "top matching snippets plus any generated response. You must use this tool to query the knowledge base."
    )
    parameters = [
        {
            "name": "query",
            "type": "string",
            "description": "Natural language question to send to the knowledge base.",
            "required": True,
        },
        {
            "name": "top_k",
            "type": "integer",
            "description": "Maximum number of candidates returned from Milvus.",
            "required": False,
        },
        {
            "name": "kb_id",
            "type": "integer",
            "description": "Target knowledge base ID.",
            "required": False,
        },
        {
            "name": "kb_name",
            "type": "string",
            "description": "Target knowledge base name.",
            "required": False,
        },
        {
            "name": "user_id",
            "type": "string",
            "description": "User identifier for auditing.",
            "required": False,
        },
        {
            "name": "max_results",
            "type": "integer",
            "description": "Number of formatted chunks to keep.",
            "required": False,
        },
        {
            "name": "request_id",
            "type": "string",
            "description": "Custom request identifier for tracing.",
            "required": False,
        },
    ]

    _PASSTHROUGH_KEYS = {
        "top_k", "kb_id", "kb_name", "user_id", "max_results", "request_id",
    }

    @classmethod
    def clear_trace(cls, request_id: str) -> None:
        """Drop any previously recorded retrieval trace for *request_id*."""
        with _TRACE_LOCK:
            _TRACE_STORE.pop(request_id, None)

    @classmethod
    def pop_trace(cls, request_id: str) -> list[dict[str, Any]]:
        """Return and remove the recorded retrieval trace for *request_id*."""
        with _TRACE_LOCK:
            return _TRACE_STORE.pop(request_id, [])

    @classmethod
    def _record_trace(cls, request_id: str, result: dict[str, Any], query: str) -> None:
        """Persist retrieval metadata so multi-hop responses can expose provenance."""
        trace_entry = {
            "query": query,
            "data": list(result.get("data", [])),
            "_topk_chunk": list(result.get("_topk_chunk", [])),
            "_chunk_data_map": dict(result.get("_chunk_data_map", {})),
            "_chunk_to_db_prefix": dict(result.get("_chunk_to_db_prefix", {})),
        }
        with _TRACE_LOCK:
            _TRACE_STORE.setdefault(request_id, []).append(trace_entry)

    def call(self, params: str | dict, **kwargs) -> str:  # type: ignore[override]
        payload = self._build_payload(params, kwargs)
        query = payload.get("query")
        if not query:
            return "Knowledge query requires a 'query' field."

        payload.setdefault("request_id", f"multi-hop-{int(time.time() * 1000)}")
        try:
            result = self._run_query(payload)
        except Exception as exc:
            return f"Knowledge query raised an exception: {exc}"

        if not result.get("success"):
            return f"Knowledge query failed: {result.get('message', 'unknown error')}"

        request_id = str(payload.get("request_id", ""))
        if request_id:
            self._record_trace(request_id, result, str(query))

        response_text = result.get("response") or "No generated answer."
        formatted_hits = self._format_hits(result.get("data", []))
        metadata = result.get("query_info", {})

        return (
            "Knowledge query succeeded.\n"
            f"Answer Draft: {response_text}\n"
            f"KB Info: {json.dumps(metadata, ensure_ascii=False)}\n"
            f"Top Hits:\n{formatted_hits}"
        )

    def _build_payload(self, params: str | dict, extra_kwargs: dict) -> dict:
        """Normalise tool params into a dict suitable for ``perform_knowledge_query``."""
        payload: dict[str, str | int] = {}
        if isinstance(params, dict):
            payload.update(params)
        elif isinstance(params, str) and params.strip():
            try:
                payload.update(json.loads(params.strip()))
            except json.JSONDecodeError:
                payload["query"] = params.strip()

        for key in self._PASSTHROUGH_KEYS:
            if key in extra_kwargs:
                payload[key] = extra_kwargs[key]

        if "query" not in payload and "messages" in extra_kwargs:
            last_msg = extra_kwargs["messages"][-1]
            payload["query"] = getattr(last_msg, "content", "")
        return payload

    def _run_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run ``perform_knowledge_query`` handling both sync and async contexts."""

        async def _runner():
            from hetadb.core.chat_processor import perform_knowledge_query
            return await perform_knowledge_query(**payload)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop — safe to use asyncio.run directly
            result = asyncio.run(_runner())
            return result if isinstance(result, dict) else {}
        # Already inside an event loop — run in a separate thread
        return self._run_in_thread(_runner)

    def _run_in_thread(self, runner) -> dict[str, Any]:
        """Execute an async runner in a daemon thread to avoid blocking the loop."""
        result_container: dict[str, Any] = {}
        error_container: dict[str, Exception] = {}

        def _target():
            try:
                result_container["result"] = asyncio.run(runner())
            except Exception as err:
                error_container["error"] = err

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join()

        if error_container:
            raise error_container["error"]
        result = result_container.get("result", {})
        return result if isinstance(result, dict) else {}

    def _format_hits(self, items: list, limit: int = 5) -> str:
        """Format retrieval hits into a compact text block for the agent."""
        if not items:
            return "No supporting chunks returned."

        snippets = []
        for idx, item in enumerate(items[:limit], start=1):
            if hasattr(item, "model_dump"):
                record = item.model_dump()
            elif isinstance(item, dict):
                record = item
            else:
                record = getattr(item, "__dict__", {})
            score = record.get("score", "n/a")
            content = record.get("content") or record.get("text") or ""
            trimmed = content.strip().replace("\n", " ")
            if len(trimmed) > MAX_SNIPPET_CHARS:
                trimmed = trimmed[: MAX_SNIPPET_CHARS - 3] + "..."
            snippets.append(f"{idx}. score={score} | content={trimmed}")
        return "\n".join(snippets)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class MultiHopAgent:
    """Orchestrates multi-hop reasoning over the knowledge base."""

    def answer(
        self,
        query: str,
        top_n: int = 10,
        score_threshold: float = 0.0,
        max_rounds: int = 3,
        collection_name: str = "Multi_hop",
        kb_id: str | None = None,
        kb_name: str | None = "test",
        user_id: str | None = "demo_user",
        max_results: int | None = 20,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run the multi-hop ReAct loop and return the reasoning trace.

        Returns:
            List of dicts, each potentially containing ``thoughts``, ``memory``,
            or ``answer`` keys from each reasoning step.
        """
        if request_id is None:
            request_id = f"multi-hop-{int(time.time() * 1000)}"

        KnowledgeQueryTool.clear_trace(request_id)

        # Copy the module-level config to avoid mutating shared state across
        # concurrent requests (race condition: two callers would overwrite each
        # other's query / action_count in the same dict).
        request_cfg = {**llm_cfg, "query": query, "action_count": max_rounds}
        bot = HAgent(llm=request_cfg, function_list=["knowledge_query"])

        retrieval_params: dict[str, Any] = {
            "top_k": top_n,
            "milvus_collection": collection_name,
            "score_threshold": score_threshold,
        }
        if kb_id is not None:
            retrieval_params["kb_id"] = kb_id
        if kb_name is not None:
            retrieval_params["kb_name"] = kb_name
        if user_id is not None:
            retrieval_params["user_id"] = user_id
        if max_results is not None:
            retrieval_params["max_results"] = max_results
        if request_id is not None:
            retrieval_params["request_id"] = request_id

        messages = [{"role": "user", "content": f"query:\n{query}"}]
        response = bot.run(messages=messages, lang="zh", **retrieval_params)

        response_jsons: list[dict[str, Any]] = []
        r = 0
        has_final_answer = False
        for i in response:
            response_json: dict[str, Any] = {}
            if '"}' in i[0]["content"] and "Memory" not in i[0]["content"]:
                thoughts_str = i[0]["content"].split("Action")[0]
                if r == 0:
                    response_json["thoughts"] = thoughts_str
                elif (
                    "thoughts" in response_jsons[r - 1]
                    and response_jsons[r - 1]["thoughts"] != thoughts_str
                ):
                    logger.debug("Step %d thoughts: %s", r - 1, response_jsons[r - 1]["thoughts"])
                    response_json["thoughts"] = thoughts_str
            elif '"}' in i[0]["content"] and "Memory" in i[0]["content"]:
                response_json["memory"] = i[0]["content"][:-2]

            if response_json:
                response_jsons.append(response_json)
                r += 1

            if "Final Answer" in i[0]["content"]:
                response_json["answer"] = i[0]["content"]
                response_jsons.append(response_json)
                has_final_answer = True

        # Final safeguard: if the loop explored and accumulated memory but did
        # not emit a formal "Final Answer", ask the critic one last time.
        if not has_final_answer and bot.momery:
            try:
                fallback_answer = bot.critic_information(query, bot.momery)
            except Exception:
                fallback_answer = None
                logger.warning("Final critic fallback failed", exc_info=True)
            if fallback_answer:
                response_jsons.append({"answer": f"Final Answer: {fallback_answer}"})

        return {
            "trace": response_jsons,
            "retrieval_trace": KnowledgeQueryTool.pop_trace(request_id),
        }
