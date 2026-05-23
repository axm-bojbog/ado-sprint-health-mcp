"""get_sprint_overview tool implementation."""
from __future__ import annotations

from typing import Optional

from ado.client import ADOClient

_DONE = frozenset({"Done", "Closed", "Resolved"})
_ACTIVE = frozenset({"Active", "In Progress", "In Review", "Committed"})
_NEW = frozenset({"New", "Ready", "Approved", "Backlog"})


async def get_sprint_overview(sprint_name: Optional[str] = None) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    items = await client.get_sprint_work_items(sprint.path)

    by_state: dict[str, int] = {}
    committed = 0.0
    completed = 0.0
    for item in items:
        by_state[item.state] = by_state.get(item.state, 0) + 1
        pts = item.story_points or 0.0
        committed += pts
        if item.state in _DONE:
            completed += pts

    completion_pct = (completed / committed * 100) if committed else 0.0

    # Rolling velocity from last 3 sprints
    past = await client.get_past_iterations(3)
    past_pts = [await client.get_completed_points(s.path) for s in past]
    avg_velocity = sum(past_pts) / len(past_pts) if past_pts else 0.0

    # Projected total based on current daily burn rate
    elapsed = sprint.days_elapsed or 1  # avoid /0 on day 0
    daily_rate = completed / elapsed
    projected = completed + daily_rate * sprint.days_remaining
    remaining_pts = committed - completed

    if committed == 0:
        likelihood = "on_track"
    elif projected >= committed * 0.9:
        likelihood = "on_track"
    elif projected >= committed * 0.7:
        likelihood = "at_risk"
    else:
        likelihood = "off_track"

    return {
        "sprint": sprint.name,
        "dates": sprint.to_dict(),
        "items": {
            "total": len(items),
            "by_state": by_state,
            "done_count": sum(by_state.get(s, 0) for s in _DONE),
            "in_progress_count": sum(by_state.get(s, 0) for s in _ACTIVE),
            "new_count": sum(by_state.get(s, 0) for s in _NEW),
        },
        "effort": {
            "committed_points": committed,
            "completed_points": completed,
            "remaining_points": remaining_pts,
            "completion_pct": round(completion_pct, 1),
        },
        "velocity": {
            "last_sprints_avg": round(avg_velocity, 1),
            "current_daily_rate": round(daily_rate, 2),
            "projected_total": round(projected, 1),
        },
        "completion_likelihood": likelihood,
    }
