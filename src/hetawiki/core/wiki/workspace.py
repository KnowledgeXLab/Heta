from __future__ import annotations

import shutil
from pathlib import Path

from hetawiki.core.wiki.git_repo import ensure_wiki_repo
from hetawiki.utils.path import WORKTREES_DIR, WIKI_DIR


def create_working_copy(task_id: str) -> Path:
    ensure_wiki_repo()
    work_root = WORKTREES_DIR / task_id
    wiki_copy = work_root / "wiki"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WIKI_DIR, wiki_copy, ignore=shutil.ignore_patterns(".git"))
    return wiki_copy


def cleanup_working_copy(task_id: str) -> None:
    work_root = WORKTREES_DIR / task_id
    if work_root.exists():
        shutil.rmtree(work_root)


def promote_working_copy(task_id: str) -> None:
    wiki_copy = WORKTREES_DIR / task_id / "wiki"
    if not wiki_copy.exists():
        raise FileNotFoundError(f"working copy does not exist for task: {task_id}")

    copy_pages = wiki_copy / "pages"
    real_pages = WIKI_DIR / "pages"
    if real_pages.exists() and copy_pages.exists():
        copy_page_names = {p.relative_to(copy_pages) for p in copy_pages.rglob("*.md")}
        for existing in real_pages.rglob("*.md"):
            if existing.relative_to(real_pages) not in copy_page_names:
                existing.unlink()

    for source in wiki_copy.rglob("*"):
        relative = source.relative_to(wiki_copy)
        target = WIKI_DIR / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def validate_working_copy(task_id: str, written_paths: set[str] | None = None) -> None:
    wiki_copy = WORKTREES_DIR / task_id / "wiki"
    index_path = wiki_copy / "index.md"
    log_path = wiki_copy / "log.md"
    pages_dir = wiki_copy / "pages"

    if not index_path.exists():
        raise ValueError("working copy is missing index.md")
    if not log_path.exists():
        raise ValueError("working copy is missing log.md")
    if not pages_dir.exists():
        raise ValueError("working copy is missing pages/")

    for path in pages_dir.rglob("*.md"):
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"page is empty: {path.relative_to(wiki_copy)}")

    for relative_path in written_paths or set():
        full_path = wiki_copy / relative_path
        if not full_path.exists():
            raise ValueError(f"written file missing from working copy: {relative_path}")
        if not full_path.read_text(encoding="utf-8").strip():
            raise ValueError(f"written file is empty: {relative_path}")
