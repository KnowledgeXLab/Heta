"""HetaWiki LLM client wrapper."""

from __future__ import annotations

import json

from json_repair import repair_json

from common.config import load_config
from common.llm_client import create_use_llm


def call_default_ingest(
    *,
    base_prompt: str,
    ingest_prompt: str,
    markdown: str,
    raw_filename: str,
    index_content: str,
) -> dict:
    cfg = load_config("hetawiki")
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg:
        raise RuntimeError("Missing `hetawiki.llm` configuration in config.yaml")

    base_url = str(llm_cfg["base_url"])
    if not base_url.endswith("/"):
        base_url += "/"

    use_llm = create_use_llm(
        url=base_url,
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        timeout=int(llm_cfg.get("timeout", 120)),
    )
    max_retries = int(llm_cfg.get("max_retries", 2))
    base_prompt_text = (
        f"{base_prompt}\n\n"
        f"{ingest_prompt}\n\n"
        f"Source filename:\n{raw_filename}\n\n"
        f"Current index.md:\n{index_content}\n\n"
        f"Parsed markdown:\n{markdown}"
    )

    last_error: str | None = None
    for attempt in range(max_retries + 1):
        if last_error:
            prompt = base_prompt_text + f"\n\nPrevious attempt failed: {last_error}\nPlease fix the issue and return valid JSON."
        else:
            prompt = base_prompt_text
        response = use_llm(prompt)
        if not response:
            last_error = "LLM returned empty response"
            continue
        try:
            return _parse_default_ingest_response(response)
        except (ValueError, Exception) as exc:
            last_error = str(exc)

    raise RuntimeError(f"HetaWiki default ingest failed after {max_retries + 1} attempts: {last_error}")


def _parse_default_ingest_response(text: str) -> dict:
    data = json.loads(repair_json(text))
    required = ("title", "category", "summary", "content")
    missing = [field for field in required if not data.get(field)]
    if missing:
        raise ValueError(f"default ingest result missing fields: {', '.join(missing)}")
    return data
