from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from hetawiki.config import load_wiki_config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_timer: threading.Timer | None = None
_enabled: bool = True
_interval_hours: int = 24
_next_run: datetime | None = None


def _run_lint_task() -> None:
    from common.tasks import TaskStatus, create_task, update_task
    from hetawiki.core.wiki.lint import run_lint

    task = create_task("hetawiki_lint", metadata={})
    update_task(task.task_id, status=TaskStatus.RUNNING, message="running lint")
    try:
        result = run_lint(task.task_id)
        from common.tasks import get_task
        t = get_task(task.task_id)
        if t is not None:
            t.metadata["result"] = result
        update_task(task.task_id, status=TaskStatus.COMPLETED, message="done", progress=1.0)
    except Exception as exc:
        logger.error("scheduled lint failed: %s", exc, exc_info=True)
        update_task(task.task_id, status=TaskStatus.FAILED, error=str(exc))
    finally:
        _schedule_next()


def _schedule_next() -> None:
    global _timer, _next_run
    with _lock:
        if not _enabled:
            _next_run = None
            return
        delay = _interval_hours * 3600
        _next_run = datetime.now() + timedelta(seconds=delay)
        _timer = threading.Timer(delay, _run_lint_task)
        _timer.daemon = True
        _timer.start()
        logger.info("lint scheduled: next run at %s", _next_run.isoformat())


def start(enabled: bool | None = None, interval_hours: int | None = None) -> None:
    global _enabled, _interval_hours
    cfg = load_wiki_config().lint
    with _lock:
        _enabled = cfg.enabled if enabled is None else enabled
        _interval_hours = cfg.interval_hours if interval_hours is None else interval_hours
    _schedule_next()


def stop() -> None:
    global _timer, _next_run
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
        _next_run = None


def update_config(
    enabled: bool | None = None,
    interval_hours: int | None = None,
) -> dict[str, Any]:
    global _enabled, _interval_hours, _timer
    with _lock:
        if enabled is not None:
            _enabled = enabled
        if interval_hours is not None:
            _interval_hours = interval_hours
        if _timer is not None:
            _timer.cancel()
            _timer = None
    _schedule_next()
    return get_status()


def get_status() -> dict[str, Any]:
    return {
        "enabled": _enabled,
        "interval_hours": _interval_hours,
        "next_run": _next_run.isoformat() if _next_run else None,
    }
