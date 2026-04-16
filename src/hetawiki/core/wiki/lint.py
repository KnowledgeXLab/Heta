from __future__ import annotations

from datetime import datetime

from hetawiki.config import load_wiki_config
from hetawiki.core.prompts import read_prompt
from hetawiki.core.wiki.agent import TOOLS, run_agent
from hetawiki.core.wiki.git_repo import commit_wiki_changes, ensure_wiki_repo, rollback_wiki_changes
from hetawiki.core.wiki.store import ensure_wiki_layout, read_index
from hetawiki.core.wiki.workspace import (
    cleanup_working_copy,
    create_working_copy,
    promote_working_copy,
    validate_working_copy,
)
from hetawiki.utils.path import WIKI_PAGES_DIR


def run_lint(task_id: str) -> dict:
    _lint_cfg = load_wiki_config().lint

    ensure_wiki_repo()
    ensure_wiki_layout()

    working_wiki = create_working_copy(task_id)

    try:
        page_list = sorted(
            str(p.relative_to(working_wiki)).replace("\\", "/")
            for p in (working_wiki / "pages").glob("*.md")
        )
        pages_summary = "\n".join(f"- {p}" for p in page_list) or "(no pages yet)"

        system = f"{read_prompt('base.md')}\n\n{read_prompt('lint.md')}"
        initial_message = (
            f"Current date:\n{datetime.now().date().isoformat()}\n\n"
            f"Current index.md:\n{read_index()}\n\n"
            f"All pages in pages/:\n{pages_summary}"
        )

        result = run_agent(
            task_id=task_id,
            system=system,
            initial_message=initial_message,
            root_dir=working_wiki,
            max_steps=_lint_cfg.max_steps,
            max_seconds=_lint_cfg.max_seconds,
            temperature=_lint_cfg.temperature,
            tools=TOOLS,
        )

        validate_working_copy(task_id, set(result["written_paths"]))
        try:
            promote_working_copy(task_id)
            committed = commit_wiki_changes("lint: health check")
        except Exception:
            rollback_wiki_changes()
            raise

        return {
            "written_paths": result["written_paths"],
            "final_response": result["final_response"],
            "committed": committed,
            "agent": result["usage"],
        }
    finally:
        cleanup_working_copy(task_id)
