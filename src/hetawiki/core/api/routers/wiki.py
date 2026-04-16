from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hetawiki.core.wiki.store import ensure_wiki_layout, read_index
from hetawiki.utils.path import WIKI_PAGES_DIR
from hetawiki.utils.text import parse_frontmatter

router = APIRouter(prefix="/api/v1/hetawiki", tags=["hetawiki"])


class IndexResponse(BaseModel):
    content: str


class PageResponse(BaseModel):
    path: str
    content: str


class GraphNode(BaseModel):
    id: str
    title: str
    category: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]



def _resolve_page(filename: str) -> Path:
    safe = Path(filename).name
    if not safe.endswith(".md"):
        safe = f"{safe}.md"
    candidate = (WIKI_PAGES_DIR / safe).resolve()
    if WIKI_PAGES_DIR.resolve() not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid page path")
    return candidate


@router.get("/index", response_model=IndexResponse)
async def get_index():
    ensure_wiki_layout()
    content = read_index()
    if not content.strip():
        raise HTTPException(status_code=404, detail="Wiki index is empty")
    return IndexResponse(content=content)


@router.get("/graph", response_model=GraphResponse)
async def get_graph():
    if not WIKI_PAGES_DIR.exists():
        return GraphResponse(nodes=[], edges=[])

    pages = list(WIKI_PAGES_DIR.glob("*.md"))

    stem_map: dict[str, GraphNode] = {}
    title_map: dict[str, GraphNode] = {}
    page_texts: dict[str, str] = {}

    for page in pages:
        text = page.read_text(encoding="utf-8")
        page_texts[page.stem] = text
        fm = parse_frontmatter(text)
        title = fm.get("title") or page.stem
        node = GraphNode(
            id=f"pages/{page.stem}",
            title=title,
            category=fm.get("category") or None,
        )
        stem_map[page.stem] = node
        title_map[title.lower()] = node

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()

    for stem, text in page_texts.items():
        source_id = f"pages/{stem}"
        for link in re.findall(r"\[\[([^\]]+)\]\]", text):
            target = link.strip()
            node = stem_map.get(target) or title_map.get(target.lower())
            if node and node.id != source_id:
                key = (source_id, node.id)
                if key not in seen:
                    seen.add(key)
                    edges.append(GraphEdge(source=source_id, target=node.id))

    return GraphResponse(nodes=list(stem_map.values()), edges=edges)


@router.get("/pages/{filename}", response_model=PageResponse)
async def get_page(filename: str):
    page_path = _resolve_page(filename)
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Page not found: {filename}")
    content = page_path.read_text(encoding="utf-8")
    return PageResponse(
        path=f"pages/{page_path.name}",
        content=content,
    )
