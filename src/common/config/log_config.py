"""
Shared logging and config utilities.

Intended for use in application entry points (main.py).  Modules should
obtain their logger via ``logging.getLogger(__name__)`` and never call
``setup_logging`` themselves.

Design notes
------------
Console output is rendered through Rich with a custom theme:

* the timestamp is dim so the eye skips past it,
* the level is a fixed-width 4-char badge (``INFO`` / ``WARN`` / ``ERR``/ ``CRIT``)
  tinted by severity,
* the logger name is tinted by *namespace* — ``hetadb.*`` mint, ``hetamem.*``
  violet, ``hetagen.*`` amber, ``hetawiki.*`` rose, third-party dim grey —
  so you can tell at a glance which subsystem is talking,
* the message body stays at default brightness and supports inline Rich
  markup (``logger.info("[bold]done[/]")``),
* paths / numbers / URLs / UUIDs are auto-highlighted by Rich's reprs.

The rotating file handler stays plain text — files are for grep, not eyes.
"""

import logging
import os
import warnings
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme
from rich.traceback import install as install_rich_tracebacks
from urllib3.exceptions import InsecureRequestWarning

# Project root: src/common/config/ → src/common/ → src/ → <project root>
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ─────────────────────────────── Theme ────────────────────────────────────

HETA_THEME = Theme(
    {
        # Frame chrome
        "log.time":              "grey50",
        "log.message":           "default",
        # Severity badges
        "log.level.debug":       "grey58",
        "log.level.info":        "#7dd3c0",
        "log.level.warning":     "#e0b050",
        "log.level.error":       "#e06b6b",
        "log.level.critical":    "bold white on #e06b6b",
        # Namespace tint for logger names
        "logger.heta":           "bold #7dd3c0",
        "logger.hetadb":         "#7dd3c0",
        "logger.hetamem":        "#c4a7e7",
        "logger.hetagen":        "#e5c07b",
        "logger.hetawiki":       "#f5a5b8",
        "logger.external":       "grey42",
        # Repr highlighter tuning — subtle, never louder than the message
        "repr.number":           "#b5c0d0",
        "repr.path":             "#8db6d5",
        "repr.filename":         "#8db6d5",
        "repr.url":              "underline #8db6d5",
        "repr.uuid":             "italic #c4a7e7",
        "repr.str":              "#a8d8c5",
        "repr.bool_true":        "italic #7dd3c0",
        "repr.bool_false":       "italic #e06b6b",
        "repr.none":             "italic grey50",
        "repr.call":             "#c4a7e7",
        "repr.attrib_name":      "grey58",
        # Banner accents
        "banner.title":          "bold #7dd3c0",
        "banner.sep":            "grey30",
        "banner.meta":           "grey50",
        "banner.value":          "#b5c0d0",
    }
)


# ─────────────────────── Level labels / namespace map ─────────────────────

_LEVEL_LABEL = {
    logging.DEBUG:    "DBG ",
    logging.INFO:     "INFO",
    logging.WARNING:  "WARN",
    logging.ERROR:    "ERR ",
    logging.CRITICAL: "CRIT",
}

_LEVEL_STYLE = {
    logging.DEBUG:    "log.level.debug",
    logging.INFO:     "log.level.info",
    logging.WARNING:  "log.level.warning",
    logging.ERROR:    "log.level.error",
    logging.CRITICAL: "log.level.critical",
}

# Order matters: longer prefixes first so "hetadb.foo" doesn't match "heta".
_NAMESPACE_STYLES = (
    ("hetadb",   "logger.hetadb"),
    ("hetamem",  "logger.hetamem"),
    ("hetagen",  "logger.hetagen"),
    ("hetawiki", "logger.hetawiki"),
    ("heta",     "logger.heta"),
)


def _style_for_logger(name: str) -> str:
    for prefix, style in _NAMESPACE_STYLES:
        if name == prefix or name.startswith(prefix + "."):
            return style
    return "logger.external"


# ─────────────────────────────── Handler ──────────────────────────────────


class _HetaRichHandler(RichHandler):
    """RichHandler with 4-char level badges and namespace-tinted logger names."""

    def get_level_text(self, record: logging.LogRecord) -> Text:
        label = _LEVEL_LABEL.get(record.levelno, record.levelname[:4].ljust(4))
        style = _LEVEL_STYLE.get(record.levelno, "log.level.info")
        return Text(label, style=style)

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        rendered = super().render_message(record, message)
        name_style = _style_for_logger(record.name)
        prefix = Text(f"{record.name}  ", style=name_style)
        return Text.assemble(prefix, rendered)


# ─────────────────────────────── Formatters ───────────────────────────────

_CONSOLE_FMT = logging.Formatter("%(message)s")

_FILE_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ─────────────────────────── Noise filters / propagation ──────────────────

_PROPAGATE_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "mcp",
    "mcp.server",
    "mcp.server.streamable_http_manager",
    "mcp.server.lowlevel.server",
    "lightrag",
    "nano-vectordb",
    "MemoryVG",
    "MemoryVG.vector_stores.milvus",
    "mineru",
    "py.warnings",
)


class _MCPAccessNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        message = record.getMessage()
        noisy_fragments = (
            "/mcp",
            "/api/v1/hetadb/files/processing/tasks",
        )
        return not any(fragment in message for fragment in noisy_fragments)


class _WarningNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "pkg_resources is deprecated as an API" in message:
            return False
        return True


def _configure_warning_filters() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API.*",
        category=UserWarning,
    )
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    logging.captureWarnings(True)


def _normalize_external_loggers() -> None:
    """Route third-party framework logs through the shared root handlers."""
    for name in _PROPAGATE_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("lightrag").setLevel(logging.INFO)


# ─────────────────────────────── Banner ───────────────────────────────────


def _print_startup_banner(
    console: Console,
    name: str,
    version: Optional[str],
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pid = os.getpid()

    parts = [f"[banner.title]{name}[/]"]
    if version:
        parts.append(f"[banner.meta]v[/][banner.value]{version}[/]")
    parts.append(f"[banner.meta]pid[/] [banner.value]{pid}[/]")
    parts.append(f"[banner.value]{ts}[/]")
    title = "  [banner.sep]•[/]  ".join(parts)

    console.print()
    console.print(Rule(title, style="banner.sep", characters="─"))
    console.print()


# ─────────────────────────────── Public API ───────────────────────────────


def setup_logging(
    name: str = "heta",
    level: int = logging.INFO,
    *,
    force: bool = False,
    version: Optional[str] = None,
    banner: bool = True,
) -> None:
    """Configure console + daily-rotated file logging for *name*.

    Both handlers are attached to the root logger so that every child logger
    (``hetadb.*``, ``hetamem.*``, third-party libs, etc.) writes to both the
    console and the log file without any extra setup.

    Args:
        name:    Log-file prefix and subdirectory name (e.g. ``"heta"``).
        level:   Root log level. Defaults to ``logging.INFO``.
        force:   If ``True``, tear down any existing root handlers first.
        version: Optional version string shown in the startup banner.
        banner:  If ``True``, print a Rich rule at startup with name/pid/time.
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return  # already configured — avoid duplicate handlers on reload
    is_reconfigure = bool(root.handlers)  # force=True on an already-configured root
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    root.setLevel(level)

    console = Console(theme=HETA_THEME, stderr=False, soft_wrap=False)

    console_handler = _HetaRichHandler(
        console=console,
        show_time=True,
        show_level=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        log_time_format="%H:%M:%S",
        omit_repeated_times=True,
        tracebacks_show_locals=False,
    )
    console_handler._log_render.level_width = 4
    console_handler.setFormatter(_CONSOLE_FMT)
    console_handler.addFilter(_MCPAccessNoiseFilter())
    console_handler.addFilter(_WarningNoiseFilter())
    root.addHandler(console_handler)

    log_dir = _PROJECT_ROOT / "log" / name
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        log_dir / f"{name}.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(_FILE_FMT)
    file_handler.addFilter(_MCPAccessNoiseFilter())
    file_handler.addFilter(_WarningNoiseFilter())
    root.addHandler(file_handler)

    _configure_warning_filters()
    _normalize_external_loggers()

    install_rich_tracebacks(console=console, show_locals=False, width=None)

    if banner and not is_reconfigure:
        _print_startup_banner(console, name, version)


def load_config(section: Optional[str] = None) -> dict:
    """Load configuration from the project-level ``config.yaml``.

    Args:
        section: Top-level key to return (e.g. ``"hetadb"``).
                 If ``None``, the full config dict is returned.

    Returns:
        The requested config section, or an empty dict if not found.
    """
    with open(_PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get(section, {}) if section else cfg
