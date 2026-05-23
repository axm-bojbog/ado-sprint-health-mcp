from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """Return dt with UTC timezone if it is naive."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Sprint:
    id: str
    name: str
    path: str
    start_date: datetime
    finish_date: datetime

    @property
    def days_total(self) -> int:
        return max(1, (self.finish_date - self.start_date).days)

    @property
    def days_remaining(self) -> int:
        remaining = (_aware(self.finish_date) - _utcnow()).days
        return max(0, remaining)

    @property
    def days_elapsed(self) -> int:
        return max(0, self.days_total - self.days_remaining)

    @property
    def is_past_midpoint(self) -> bool:
        return self.days_elapsed >= self.days_total / 2

    @property
    def is_in_last_third(self) -> bool:
        return self.days_elapsed >= self.days_total * 2 / 3

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "start_date": self.start_date.isoformat(),
            "finish_date": self.finish_date.isoformat(),
            "days_total": self.days_total,
            "days_remaining": self.days_remaining,
            "days_elapsed": self.days_elapsed,
            "past_midpoint": self.is_past_midpoint,
            "in_last_third": self.is_in_last_third,
        }


@dataclass
class WorkItem:
    id: int
    title: str
    work_item_type: str
    state: str
    assigned_to: Optional[str]
    story_points: Optional[float]
    iteration_path: str
    changed_date: datetime
    created_date: datetime
    state_change_date: Optional[datetime]
    tags: list[str] = field(default_factory=list)
    priority: Optional[int] = None
    relations: list[dict] = field(default_factory=list)

    def has_pr_link(self) -> bool:
        for rel in self.relations:
            url = rel.get("url", "")
            if "GitPullRequest" in url or "pullrequest" in url.lower():
                return True
        return False

    def is_blocked(self) -> bool:
        return any(t.strip().lower() == "blocked" for t in self.tags)

    def days_in_current_state(self) -> int:
        ref = self.state_change_date or self.changed_date
        return max(0, (_utcnow() - _aware(ref)).days)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.work_item_type,
            "state": self.state,
            "assigned_to": self.assigned_to,
            "story_points": self.story_points,
            "iteration_path": self.iteration_path,
            "changed_date": self.changed_date.isoformat(),
            "tags": self.tags,
            "priority": self.priority,
        }
