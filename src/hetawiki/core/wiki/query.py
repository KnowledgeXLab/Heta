from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hetawiki.config import load_wiki_config
from hetawiki.core.prompts import read_prompt
from hetawiki.core.wiki.agent import TOOLS, run_agent
from hetawiki.core.wiki.git_repo import commit_wiki_changes, ensure_wiki_repo
from hetawiki.core.wiki.store import ensure_wiki_layout, read_index, update_index
from hetawiki.utils.path import WIKI_DIR
from hetawiki.utils.text import parse_frontmatter

_QUERY_TOOL_NAMES = {"read_page", "create_page", "edit_page"}


def run_query(question: str, task_id: str) -> dict:
    _query_cfg = load_wiki_config().query

    ensure_wiki_repo()
    ensure_wiki_layout()

    system = f"{read_prompt('base.md')}\n\n{read_prompt('query.md')}"
    initial_message = (
        f"Question:\n{question}\n\n"
        f"Current date:\n{datetime.now().date().isoformat()}\n\n"
        f"Current index.md:\n{read_index()}"
    )

    query_tools = [t for t in TOOLS if t["function"]["name"] in _QUERY_TOOL_NAMES]

    result = run_agent(
        task_id=task_id,
        system=system,
        initial_message=initial_message,
        root_dir=WIKI_DIR,
        max_steps=_query_cfg.max_steps,
        max_seconds=_query_cfg.max_seconds,
        temperature=_query_cfg.temperature,
        tools=query_tools,
    )

    sources = []
    for path in result["read_paths"]:
        full = WIKI_DIR / path
        if full.exists():
            fm = parse_frontmatter(full.read_text(encoding="utf-8"))
            title = fm.get("title") or Path(path).stem
            sources.append(title)

    archived = None
    if result["written_paths"]:
        archived_path = result["written_paths"][0]
        full = WIKI_DIR / archived_path
        if full.exists():
            fm = parse_frontmatter(full.read_text(encoding="utf-8"))
            title = fm.get("title") or Path(archived_path).stem
            summary = fm.get("summary", "")
            update_index(
                title=title,
                category="synthesis",
                summary=summary,
                page_path=archived_path,
            )
            archived = title
        commit_wiki_changes(f"query: archive {Path(archived_path).stem}")

    return {
        "answer": result["final_response"],
        "sources": sources,
        "archived": archived,
    }
