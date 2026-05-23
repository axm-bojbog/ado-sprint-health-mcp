"""get_team_load tool implementation."""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ado.client import ADOClient
from config import settings

_DONE = frozenset({"Done", "Closed", "Resolved"})
_IN_REVIEW = frozenset({"In Review", "Review"})


async def get_team_load(sprint_name: Optional[str] = None) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    items = await client.get_sprint_work_items(sprint.path)

    by_assignee: dict[str, list] = defaultdict(list)
    unassigned: list = []
    for item in items:
        if item.assigned_to:
            by_assignee[item.assigned_to].append(item)
        else:
            unassigned.append(item)

    # Team average over assigned work only
    total_committed = sum(
        item.story_points or 0
        for member_items in by_assignee.values()
        for item in member_items
        if item.state not in _DONE
    )
    member_count = max(len(by_assignee), 1)
    team_avg = total_committed / member_count

    members: list[dict] = []
    for assignee, assignee_items in sorted(by_assignee.items()):
        committed = sum(i.story_points or 0 for i in assignee_items if i.state not in _DONE)
        completed = sum(i.story_points or 0 for i in assignee_items if i.state in _DONE)
        in_review = sum(1 for i in assignee_items if i.state in _IN_REVIEW)

        # An item is "overdue" if it has been in an active state longer than
        # the sprint has been running — a rough proxy for items slipping past
        # their implicit deadline.
        overdue = sum(
            1
            for i in assignee_items
            if i.state not in _DONE
            and i.days_in_current_state() > sprint.days_elapsed
        )

        ratio = (committed / team_avg) if team_avg else 1.0
        if ratio > settings.overload_multiplier:
            classification = "over_allocated"
        elif ratio < settings.underload_multiplier:
            classification = "under_allocated"
        else:
            classification = "balanced"

        members.append({
            "display_name": assignee,
            "committed_points": committed,
            "completed_points": completed,
            "completion_pct": round(completed / (committed + completed) * 100, 1)
            if (committed + completed)
            else 0.0,
            "item_count": len(assignee_items),
            "in_review_count": in_review,
            "overdue_count": overdue,
            "load_ratio": round(ratio, 2),
            "load_classification": classification,
        })

    # Sort: over_allocated → balanced → under_allocated; within each, descending by points
    _order = {"over_allocated": 0, "balanced": 1, "under_allocated": 2}
    members.sort(
        key=lambda m: (_order[m["load_classification"]], -m["committed_points"])
    )

    return {
        "sprint": sprint.name,
        "team_avg_committed_points": round(team_avg, 1),
        "unassigned_item_count": len(unassigned),
        "member_count": len(members),
        "team_members": members,
    }
