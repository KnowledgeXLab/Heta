from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from hetawiki.core.wiki import scheduler

router = APIRouter(prefix="/api/v1/hetawiki", tags=["hetawiki"])


class LintConfigRequest(BaseModel):
    interval_hours: int | None = None
    enabled: bool | None = None


class LintConfigResponse(BaseModel):
    interval_hours: int
    enabled: bool
    next_run: str | None


@router.post("/lint", response_model=LintConfigResponse)
async def update_lint_config(request: LintConfigRequest):
    status = scheduler.update_config(
        enabled=request.enabled,
        interval_hours=request.interval_hours,
    )
    return LintConfigResponse(**status)
