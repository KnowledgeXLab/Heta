"""HetaWiki prompt templates."""

from pathlib import Path

from hetawiki.utils.path import PROMPTS_DIR


def read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return Path(path).read_text(encoding="utf-8").strip()
