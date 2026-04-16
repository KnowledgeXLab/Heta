"""Project path definitions for hetawiki module."""

from pathlib import Path

# Project root directory (Heta/)
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Package directory (hetawiki/)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# HetaWiki workspace directory
HETAWIKI_WORKSPACE = PROJECT_ROOT / "workspace" / "hetawiki"
WORKTREES_DIR = HETAWIKI_WORKSPACE / ".worktrees"

# Raw source file directory
RAW_DIR = HETAWIKI_WORKSPACE / "raw"

# Versioned wiki directory
WIKI_DIR = HETAWIKI_WORKSPACE / "wiki"
WIKI_PAGES_DIR = WIKI_DIR / "pages"
INDEX_PATH = WIKI_DIR / "index.md"
LOG_PATH = WIKI_DIR / "log.md"

# Prompt files bundled with the package
PROMPTS_DIR = PACKAGE_ROOT / "core" / "prompts"
