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

SUPPORTED_INSERT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".html",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp", ".ico",
    ".txt", ".text", ".md", ".markdown",
    ".csv", ".xls", ".xlsx", ".ods",
}

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


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
