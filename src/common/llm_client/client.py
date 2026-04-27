"""
LLM / VLM client factories.

Each factory returns a callable (sync or async) that wraps the OpenAI-compatible
chat/completions API with retry, concurrency control, and <think> tag stripping.
"""

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Awaitable, Callable

from json_repair import repair_json

import requests
from httpx import AsyncClient, HTTPStatusError, RequestError

logger = logging.getLogger(__name__)

_THINK_RE_CLOSED = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_RE_OPEN = re.compile(r"<think>.*", re.DOTALL)


def _normalise_base_url(url: str) -> str:
    """Return a base URL that is safe for OpenAI-compatible endpoint joins."""
    return url.rstrip("/") + "/"


def _join_endpoint(base_url: str, endpoint: str) -> str:
    """Join API base URLs without requiring config authors to manage slashes."""
    return _normalise_base_url(base_url) + endpoint.lstrip("/")


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks (including unclosed ones) from LLM output."""
    text = _THINK_RE_CLOSED.sub("", text).strip()
    text = _THINK_RE_OPEN.sub("", text).strip()
    return text


def parse_nodes(response: str) -> list[dict]:
    """Parse a JSON string (possibly wrapped in markdown fences) into a list of dicts."""
    try:
        response = response.strip()
        if response.startswith("```"):
            lines = response.split("\n")
            start = 1 if lines[0].strip().startswith("```") else 0
            end = -1 if lines[-1].strip().startswith("```") else len(lines)
            response = "\n".join(lines[start:end])

        data = json.loads(repair_json(response))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception as e:
        logger.warning("Failed to parse nodes: %s", e)
        return []


# ---------------------------------------------------------------------------
# Sync LLM client
# ---------------------------------------------------------------------------

def create_use_llm(
    url: str,
    api_key: str,
    model: str = "qwen",
    timeout: int = 120,
    max_retries: int = 3,
) -> Callable[..., str]:
    """Return a synchronous ``use_llm(prompt, **kwargs) -> str`` callable."""
    endpoint = _join_endpoint(url, "chat/completions")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    def use_llm(prompt: str, **kwargs: Any) -> str:
        messages = [{"role": "user", "content": prompt + "/no_think"}]

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    endpoint,
                    headers=headers,
                    json={"model": model, "messages": messages, "stream": False, "enable_thinking": False, **kwargs},
                    timeout=timeout,
                    verify=False,
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                return _strip_think_tags(text)

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status != 429 and status < 500:
                    logger.error("LLM non-retryable HTTP %d error: %s", status, e)
                    return ""
                logger.warning("LLM request failed (attempt %d/%d): HTTP %d", attempt + 1, max_retries, status)
            except requests.exceptions.RequestException as e:
                logger.warning("LLM request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
            except Exception as e:
                logger.error("Unexpected LLM error: %s", e)
                return ""

            if attempt < max_retries - 1:
                time.sleep(random.uniform(0, 2 ** attempt))

        logger.error("LLM request failed after %d retries, returning empty", max_retries)
        return ""

    return use_llm


# ---------------------------------------------------------------------------
# Async LLM client
# ---------------------------------------------------------------------------

def create_use_llm_async(
    url: str,
    api_key: str,
    model: str = "qwen",
    timeout: int = 120,
    max_retries: int = 3,
    max_concurrent_requests: int = 5,
) -> Callable[..., Awaitable[str]]:
    """Return an async ``use_llm(prompt, **kwargs) -> str`` callable with concurrency control."""
    client = AsyncClient(
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        base_url=_normalise_base_url(url),
        timeout=timeout,
        verify=False,
    )
    sem = asyncio.Semaphore(max_concurrent_requests)

    async def use_llm(prompt: str, **kwargs: Any) -> str:
        messages = [{"role": "user", "content": prompt + "/no_think"}]

        for attempt in range(max_retries):
            try:
                async with sem:
                    resp = await client.post(
                        "/chat/completions",
                        json={"model": model, "messages": messages, "stream": False, "enable_thinking": False, **kwargs},
                    )
                    resp.raise_for_status()
                    text = resp.json()["choices"][0]["message"]["content"]
                    return _strip_think_tags(text)

            except HTTPStatusError as e:
                status = e.response.status_code
                if status != 429 and status < 500:
                    logger.error("LLM non-retryable HTTP %d error: %s", status, e)
                    return ""
                logger.warning("LLM request failed (attempt %d/%d): HTTP %d", attempt + 1, max_retries, status)
            except (RequestError, ValueError, KeyError) as e:
                logger.warning("LLM request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
            except Exception as e:
                logger.error("Unexpected LLM error: %s", e)
                return ""

            if attempt < max_retries - 1:
                await asyncio.sleep(random.uniform(0, 2 ** attempt))

        logger.error("LLM request failed after %d retries, returning empty", max_retries)
        return ""

    return use_llm


# ---------------------------------------------------------------------------
# Async VLM client
# ---------------------------------------------------------------------------

def create_use_vlm(
    url: str,
    api_key: str,
    model: str = "qwen-vl",
    timeout: int = 120,
    max_retries: int = 3,
    max_concurrent_requests: int = 5,
) -> Callable[[str, str, str], Awaitable[str]]:
    """Return an async ``use_vlm(prompt, base64_img, mime_type) -> str`` callable."""
    client = AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        base_url=_normalise_base_url(url),
        timeout=timeout,
        verify=False,
    )
    sem = asyncio.Semaphore(max_concurrent_requests)

    async def use_vlm(text_prompt: str, base64_str: str, mime_type: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_str}"}},
                    {"type": "text", "text": text_prompt},
                ],
            }
        ]

        for attempt in range(max_retries):
            try:
                async with sem:
                    resp = await client.post(
                        "/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": 0.1,
                            "stream": False,
                            "enable_thinking": False,
                            "vl_high_resolution_images": True,
                        },
                    )
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    if content is None:
                        raise ValueError("VLM returned null content")
                    return content

            except HTTPStatusError as e:
                status = e.response.status_code
                if status != 429 and status < 500:
                    logger.error("VLM non-retryable HTTP %d error: %s", status, e)
                    return ""
                logger.warning("VLM request failed (attempt %d/%d): HTTP %d", attempt + 1, max_retries, status)
            except (RequestError, ValueError, KeyError) as e:
                logger.warning("VLM request failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
            except Exception as e:
                logger.error("Unexpected VLM error: %s", e)
                return ""

            if attempt < max_retries - 1:
                await asyncio.sleep(random.uniform(0, 2 ** attempt))

        logger.error("VLM request failed after %d retries, returning empty", max_retries)
        return ""

    return use_vlm
