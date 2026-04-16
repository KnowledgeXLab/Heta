from __future__ import annotations

import re
from datetime import datetime

import yaml


def slugify(title: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", " "} else "" for ch in title)
    cleaned = "-".join(cleaned.split()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)   # collapse --- → -
    return cleaned or datetime.now().strftime("page-%Y%m%d%H%M%S")


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
