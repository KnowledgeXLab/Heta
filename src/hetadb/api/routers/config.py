"""Config API router.

Provides read-only access to project configuration via REST endpoints.
Configuration is loaded from:
  - ``config.yaml`` (project root)
  - ``src/hetadb/config/db_config.yaml``

All business logic is in :mod:`hetadb.core.config_processor`.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hetadb.core.config_processor import (
    get_full_config,
    get_config_section,
    reload_config,
)

router = APIRouter(prefix="/api/v1/hetadb/config", tags=["config"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ConfigResponse(BaseModel):
    """Standard config response envelope."""
    success: bool
    message: str
    data: dict | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=ConfigResponse)
async def get_config():
    """Get the complete merged configuration.

    Returns all config sections from ``config.yaml`` and ``db_config.yaml``
    merged into a single dictionary.
    """
    try:
        config = get_full_config()
        return ConfigResponse(
            success=True,
            message="Config retrieved successfully",
            data=config,
        )
    except Exception as e:
        logger.error("Failed to get config: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load config: {e}")


@router.get("/{section}", response_model=ConfigResponse)
async def get_section(section: str):
    """Get a specific configuration section.

    Args:
        section: Section name (e.g., "llm", "postgresql", "chunk_rechunk").

    Returns:
        The requested config section wrapped in a response envelope.
    """
    try:
        value = get_config_section(section)
        return ConfigResponse(
            success=True,
            message=f"Section '{section}' retrieved successfully",
            data={section: value},
        )
    except KeyError as e:
        logger.warning("Config section not found: %s", section)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to get config section '%s': %s", section, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load section: {e}")


@router.post("/reload", response_model=ConfigResponse)
async def reload():
    """Reload configuration from disk.

    Clears the cache and re-reads config files. Use this after manually
    editing ``config.yaml`` or ``db_config.yaml`` to refresh the cached values.
    """
    try:
        reload_config()
        return ConfigResponse(
            success=True,
            message="Config reloaded successfully",
            data=None,
        )
    except Exception as e:
        logger.error("Failed to reload config: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to reload config: {e}")
