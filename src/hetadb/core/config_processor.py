"""Config service layer.

Loads and merges project configuration from:
  - ``config.yaml`` (project root) - main hetadb config
  - ``src/hetadb/config/db_config.yaml`` - processing parameters

Provides read-only access with simple caching.
"""

import logging
from threading import Lock
from typing import Any

import yaml

from hetadb.utils.path import PROJECT_ROOT, PACKAGE_ROOT

logger = logging.getLogger(__name__)

_config_cache: dict[str, Any] | None = None
_cache_lock = Lock()


def _load_configs() -> dict[str, Any]:
    """Load and merge all config files into a unified structure.

    Returns:
        Merged config dict with keys from ``config.yaml`` plus stage-oriented
        sections from ``db_config.yaml`` such as ``parse``,
        ``chunk_rechunk``, ``graph_extraction``, and ``graph_dedup``.
    """
    merged = {}

    # 1. Load main config from project root
    main_config_path = PROJECT_ROOT / "config.yaml"
    try:
        with open(main_config_path, encoding="utf-8") as f:
            main_cfg = yaml.safe_load(f)
            hetadb_cfg = main_cfg.get("hetadb", {})

            # Copy all top-level hetadb sections
            for key, value in hetadb_cfg.items():
                merged[key] = value

        logger.debug("Loaded main config from %s", main_config_path)
    except Exception as e:
        logger.error("Failed to load main config: %s", e)
        raise

    # 2. Load DB processing config
    db_config_path = PACKAGE_ROOT / "config" / "db_config.yaml"
    try:
        with open(db_config_path, encoding="utf-8") as f:
            db_cfg = yaml.safe_load(f)

            # Extract parameter sections
            param = db_cfg.get("parameter", {})
            for section in ("parse", "chunk_rechunk", "graph_extraction", "graph_dedup"):
                if section in param:
                    merged[section] = param[section]

            # Also include postgres_batch_size if present
            if "postgres_batch_size" in db_cfg:
                merged["postgres_batch_size"] = db_cfg["postgres_batch_size"]
            if "parse_max_workers" in db_cfg:
                merged["parse_max_workers"] = db_cfg["parse_max_workers"]

        logger.debug("Loaded DB config from %s", db_config_path)
    except Exception as e:
        logger.error("Failed to load DB config: %s", e)
        raise

    return merged


def get_full_config() -> dict[str, Any]:
    """Get the complete merged configuration.

    Loads from cache if available, otherwise reads from disk and caches.

    Returns:
        Complete config dict with all sections merged.
    """
    global _config_cache

    with _cache_lock:
        if _config_cache is None:
            _config_cache = _load_configs()
            logger.info("Config loaded and cached")
        return dict(_config_cache)


def get_config_section(section: str) -> Any:
    """Get a specific configuration section.

    Args:
        section: Section name (e.g., "llm", "postgresql", "chunk_rechunk").

    Returns:
        The config value for that section.

    Raises:
        KeyError: if section does not exist.
    """
    config = get_full_config()
    if section not in config:
        raise KeyError(f"Config section '{section}' not found")
    return config[section]


def reload_config() -> None:
    """Reload configuration from disk, clearing the cache.

    Call this after manually editing config files to refresh the cached values.
    """
    global _config_cache

    with _cache_lock:
        _config_cache = None
        logger.info("Config cache cleared")

    # Trigger reload
    get_full_config()
    logger.info("Config reloaded from disk")
