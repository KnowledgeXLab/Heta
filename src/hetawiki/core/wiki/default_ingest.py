from __future__ import annotations

from pathlib import Path

from hetawiki.core.llm import call_default_ingest
from hetawiki.core.prompts import read_prompt
from hetawiki.core.wiki.git_repo import commit_wiki_changes, ensure_wiki_repo
from hetawiki.core.wiki.store import append_log, build_page_markdown, read_index, update_index, write_page


def run_default_ingest(markdown: str, raw_path: Path) -> dict:
    ensure_wiki_repo()

    try:
        result = call_default_ingest(
            base_prompt=read_prompt("base.md"),
            ingest_prompt=read_prompt("ingest.md"),
            markdown=markdown,
            raw_filename=raw_path.name,
            index_content=read_index(),
        )
    except RuntimeError:
        result = {
            "title": raw_path.stem,
            "category": "unprocessed",
            "summary": "LLM processing failed; raw content preserved.",
            "content": markdown,
        }

    title = str(result["title"]).strip()
    category = str(result["category"]).strip()
    summary = str(result["summary"]).strip()
    content = str(result["content"]).strip()

    page_markdown = build_page_markdown(
        title=title,
        summary=summary,
        content=content,
        source_filename=raw_path.name,
    )
    page_path = write_page(title, page_markdown)
    update_index(title=title, category=category, summary=summary, page_path=page_path)
    append_log(f"ingest | {raw_path.name} -> {page_path}")
    committed = commit_wiki_changes(f"ingest: {title}")

    return {
        "title": title,
        "category": category,
        "summary": summary,
        "page_path": page_path,
        "committed": committed,
    }
