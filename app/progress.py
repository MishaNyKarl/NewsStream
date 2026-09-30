"""Transport-independent events from actual work, never estimated percentages."""
import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    data: dict = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]
progress_context: ContextVar[ProgressCallback | None] = ContextVar("progress_callback", default=None)


async def report(stage, **data):
    callback = progress_context.get()
    if callback is not None:
        try:
            async with asyncio.timeout(1):
                await callback(ProgressEvent(stage, data))
        except Exception as exc:
            # Optional UI must not interrupt a check or reveal provider secrets.
            log.warning("progress_callback_failed error_type=%s", type(exc).__name__)


@dataclass
class CheckOutcome:
    status: str
    sources: int = 0
    partial_search: bool = False
    message: str = ""


@dataclass
class CheckMetrics:
    queries_failed: int = 0
