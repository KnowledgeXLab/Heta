from __future__ import annotations

import threading

import git

from hetawiki.utils.path import INDEX_PATH, LOG_PATH, WIKI_DIR, WIKI_PAGES_DIR

_COMMIT_LOCK = threading.Lock()


def ensure_wiki_repo() -> git.Repo:
    WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        INDEX_PATH.write_text("# Wiki Index\n\n", encoding="utf-8")
    if not LOG_PATH.exists():
        LOG_PATH.write_text("# Wiki Log\n\n", encoding="utf-8")

    if (WIKI_DIR / ".git").exists():
        repo = git.Repo(WIKI_DIR)
    else:
        repo = git.Repo.init(WIKI_DIR)

    with repo.config_writer() as writer:
        writer.set_value("user", "name", "HetaWiki")
        writer.set_value("user", "email", "hetawiki@local")
    return repo


def rollback_wiki_changes() -> None:
    repo = ensure_wiki_repo()
    with _COMMIT_LOCK:
        if not repo.is_dirty(untracked_files=True):
            return
        repo.git.checkout("--", ".")
        repo.git.clean("-fd")


def commit_wiki_changes(message: str) -> bool:
    repo = ensure_wiki_repo()
    with _COMMIT_LOCK:
        if not repo.is_dirty(untracked_files=True):
            return False
        repo.git.add(A=True)
        repo.index.commit(message)
    return True
