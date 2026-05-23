"""find_at_risk_items tool implementation."""
from __future__ import annotations

from typing import Optional

from ado.client import ADOClient
from config import settings

_DONE = frozenset({"Done", "Closed", "Resolved", "Removed"})
_ACTIVE = frozenset({"Active", "In Progress", "Committed", "In Review"})
_NOT_STARTED = frozenset({"New", "Ready", "Approved", "Backlog"})


async def find_at_risk_items(sprint_name: Optional[str] = None) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    items = await client.get_sprint_work_items(sprint.path, expand_relations=True)

    stale_threshold = settings.at_risk_stale_days
    at_risk: list[dict] = []

    for item in items:
        if item.state in _DONE:
            continue

        reasons: list[dict] = []

        # Rule 1 — stale in active state
        if item.state in _ACTIVE:
            days = item.days_in_current_state()
            if days > stale_threshold:
                reasons.append({
                    "type": "stale_in_progress",
                    "detail": (
                        f"In '{item.state}' for {days} day{'s' if days != 1 else ''} "
                        f"without a state change (threshold: {stale_threshold}d)"
                    ),
                })

        # Rule 2 — assigned and active past midpoint but no PR linked
        if (
            sprint.is_past_midpoint
            and item.state in _ACTIVE
            and item.assigned_to
            and not item.has_pr_link()
        ):
            reasons.append({
                "type": "no_pr_past_midpoint",
                "detail": (
                    "Item is active and assigned past sprint midpoint "
                    "but has no linked pull request"
                ),
            })

        # Rule 3 — high-effort item still unstarted in last third
        if (
            sprint.is_in_last_third
            and item.state in _NOT_STARTED
            and (item.story_points or 0) >= 8
        ):
            reasons.append({
                "type": "high_effort_not_started",
                "detail": (
                    f"{item.story_points} pt item has not been started "
                    "and the sprint is in its final third"
                ),
            })

        # Rule 4 — explicitly blocked via tag
        if item.is_blocked():
            reasons.append({
                "type": "blocked",
                "detail": "Tagged as 'Blocked'",
            })

        if reasons:
            at_risk.append({
                **item.to_dict(),
                "risk_reasons": reasons,
                "risk_score": len(reasons),
            })

    at_risk.sort(key=lambda x: (-x["risk_score"], -(x.get("story_points") or 0)))

    return {
        "sprint": sprint.name,
        "at_risk_count": len(at_risk),
        "sprint_past_midpoint": sprint.is_past_midpoint,
        "sprint_in_last_third": sprint.is_in_last_third,
        "heuristics_applied": [
            f"stale_in_progress (>{stale_threshold}d without state change)",
            "no_pr_past_midpoint",
            "high_effort_not_started (>=8 pts in last third)",
            "blocked (tag)",
        ],
        "items": at_risk,
    }
