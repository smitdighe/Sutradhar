"""APScheduler lifecycle. Jobs are registered by the worker modules; none yet."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the process scheduler, or ``None`` when it was never started."""
    return _scheduler


def start_scheduler() -> AsyncIOScheduler | None:
    """Start the scheduler unless ``SCHEDULER_ENABLED`` is false."""
    global _scheduler
    if not get_settings().scheduler_enabled:
        return None
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    if not _scheduler.running:
        _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler if it is running."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


__all__ = ["get_scheduler", "shutdown_scheduler", "start_scheduler"]
