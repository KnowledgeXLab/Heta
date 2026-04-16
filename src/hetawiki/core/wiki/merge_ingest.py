from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hetawiki.config import load_wiki_config
from hetawiki.core.prompts import read_prompt
from hetawiki.core.wiki.agent import run_agent
from hetawiki.core.wiki.git_repo import commit_wiki_changes, ensure_wiki_repo, rollback_wiki_changes
from hetawiki.core.wiki.store import read_index
from hetawiki.core.wiki.workspace import (
    cleanup_working_copy,
    create_working_copy,
    promote_working_copy,
    validate_working_copy,
)


def run_merge_ingest(markdown: str, raw_path: Path, task_id: str) -> dict:
    _merge_cfg = load_wiki_config().merge

    ensure_wiki_repo()
    working_wiki = create_working_copy(task_id)

    try:
        system = f"{read_prompt('base.md')}\n\n{read_prompt('ingest-merge.md')}"
        initial_message = (
            f"Source filename:\n{raw_path.name}\n\n"
            f"Current date:\n{datetime.now().date().isoformat()}\n\n"
            f"Current index.md:\n{read_index()}\n\n"
            f"Parsed markdown:\n{markdown}"
        )
        result = run_agent(
            task_id=task_id,
            system=system,
            initial_message=initial_message,
            root_dir=working_wiki,
            max_steps=_merge_cfg.max_steps,
            max_seconds=_merge_cfg.max_seconds,
            temperature=_merge_cfg.temperature,
        )
        validate_working_copy(task_id, set(result["written_paths"]))
        try:
            promote_working_copy(task_id)
            committed = commit_wiki_changes(f"merge: {raw_path.stem}")
        except Exception:
            rollback_wiki_changes()
            raise
        return {
            "mode": "merge",
            "written_paths": result["written_paths"],
            "final_response": result["final_response"],
            "committed": committed,
            "agent": result["usage"],
        }
    finally:
        cleanup_working_copy(task_id)
