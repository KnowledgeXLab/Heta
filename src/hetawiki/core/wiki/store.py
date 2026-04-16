from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

_INDEX_LOCK = threading.Lock()

from hetawiki.utils.path import INDEX_PATH, LOG_PATH, RAW_DIR, WIKI_PAGES_DIR
from hetawiki.utils.text import slugify


def ensure_raw_dir() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    return RAW_DIR


def save_raw_upload(filename: str, fileobj: BinaryIO) -> Path:
    raw_dir = ensure_raw_dir()
    safe_name = Path(filename).name or "upload.bin"
    dated_name = f"{datetime.now():%Y-%m-%d_%H%M%S}_{safe_name}"
    target = _resolve_collision(raw_dir, dated_name)

    try:
        with target.open("wb") as f:
            while chunk := fileobj.read(1024 * 1024):
                f.write(chunk)
    except Exception:
        if target.exists():
            target.unlink()
        raise

    return target


def _resolve_collision(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for counter in range(1, 1000):
        candidate = directory / f"{stem}({counter}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many files with the same name: {filename}")


def ensure_wiki_layout() -> None:
    WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        INDEX_PATH.write_text("# Wiki Index\n\n", encoding="utf-8")
    if not LOG_PATH.exists():
        LOG_PATH.write_text("# Wiki Log\n\n", encoding="utf-8")


def build_page_markdown(
    *,
    title: str,
    summary: str,
    content: str,
    source_filename: str,
) -> str:
    today = datetime.now().date().isoformat()
    body = content.strip() or "No content."
    return (
        "---\n"
        f"title: {title.strip()}\n"
        f"sources: [{source_filename}]\n"
        f"updated: {today}\n"
        "---\n\n"
        "## Summary\n"
        f"{summary.strip()}\n\n"
        "## Content\n"
        f"{body}\n\n"
        "## Related Pages\n"
        "- None yet\n"
    )


def write_page(title: str, content: str) -> str:
    ensure_wiki_layout()
    slug = slugify(title)
    page_path = WIKI_PAGES_DIR / f"{slug}.md"
    if page_path.exists():
        page_path = WIKI_PAGES_DIR / f"{slug}-{datetime.now():%Y%m%d%H%M%S}.md"
    try:
        page_path.write_text(content, encoding="utf-8")
    except Exception:
        if page_path.exists():
            page_path.unlink()
        raise
    return str(page_path.relative_to(WIKI_PAGES_DIR.parent)).replace("\\", "/")


def read_index() -> str:
    ensure_wiki_layout()
    return INDEX_PATH.read_text(encoding="utf-8")


def update_index(*, title: str, category: str, summary: str, page_path: str) -> None:
    ensure_wiki_layout()
    with _INDEX_LOCK:
        existing = INDEX_PATH.read_text(encoding="utf-8").rstrip()
        category = category.strip() or "Uncategorized"
        entry = f"- [[{title.strip()}]] ({page_path}) — {summary.strip()}"

        lines = existing.splitlines()
        if not lines:
            lines = ["# Wiki Index"]

        header = f"## {category}"
        if header in lines:
            insert_at = lines.index(header) + 1
            while insert_at < len(lines) and not lines[insert_at].startswith("## "):
                insert_at += 1
            lines.insert(insert_at, entry)
        else:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([header, entry])

        INDEX_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def append_log(message: str) -> None:
    ensure_wiki_layout()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"- [{timestamp}] {message}\n")


