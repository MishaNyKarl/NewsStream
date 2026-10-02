"""Monitoring policy shared by Telegram, workers and a future HTTP API."""
from datetime import timedelta

from app.domain import UserError

INTENSIVE_CHECKPOINTS = tuple(timedelta(minutes=m) for m in (30, 60, 120, 240, 480, 720, 1440))
INTENSIVE_DURATION = timedelta(hours=24)


class IntensiveSlotOccupied(UserError):
    def __init__(self, story_id, title):
        super().__init__("Все слоты срочных наблюдений вашего тарифа заняты.")
        self.story_id = story_id
        self.title = title


def next_checkpoint(started_at, now):
    """Skip missed checkpoints after downtime; never issue a catch-up burst."""
    return next((started_at + step for step in INTENSIVE_CHECKPOINTS if started_at + step > now), None)


def completion_next(story, now, error=False):
    # Manual checks and a mode change during an in-flight check must not postpone
    # an already planned future check. The persisted schedule is authoritative.
    if story.next_check_at is not None and story.next_check_at > now:
        return story.next_check_at
    intensive = story.monitoring_mode == "intensive" and story.intensive_until > now
    if intensive:
        planned = next_checkpoint(story.intensive_started_at, now)
        return min(planned, now + timedelta(minutes=5)) if error else planned
    return now + (timedelta(minutes=30) if error else timedelta(hours=story.check_frequency_hours))
