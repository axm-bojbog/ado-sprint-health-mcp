"""forecast_sprint_completion tool implementation.

Algorithm
---------
1. Done items:  already complete — count toward projected total.
2. In-progress items:
   - Not stale (days_in_state <= threshold) → likely to complete.
   - Stale                                  → at risk; excluded from projection.
3. Not-started items:
   - Remaining capacity = effective_daily_rate × remaining_days − likely_active_pts.
   - Items are ordered by priority (asc) then story points (asc, "quick wins first").
   - Items that fit capacity are projected to complete; others are flagged as unlikely.
4. Effective daily rate:
   - Sprint is young (<25 % elapsed) → use historical (past 3 sprint avg / sprint days).
   - Sprint is mid (25–60 % elapsed) → weighted blend (40 % hist, 60 % current).
   - Sprint is late (>60 % elapsed)  → trust current sprint burn rate.
   - No historical data available    → current sprint rate only.
"""
from __future__ import annotations

from typing import Optional

from ado.client import ADOClient
from config import settings

_DONE = frozenset({"Done", "Closed", "Resolved"})
_ACTIVE = frozenset({"Active", "In Progress", "Committed", "In Review"})


async def forecast_sprint_completion(sprint_name: Optional[str] = None) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    items = await client.get_sprint_work_items(sprint.path)

    done_items = [i for i in items if i.state in _DONE]
    active_items = [i for i in items if i.state in _ACTIVE]
    # Anything not done and not active is "not started" (covers New, Ready, Backlog, etc.)
    not_started_items = [i for i in items if i.state not in _DONE and i.state not in _ACTIVE]

    done_pts = sum(i.story_points or 0 for i in done_items)
    active_pts = sum(i.story_points or 0 for i in active_items)
    not_started_pts = sum(i.story_points or 0 for i in not_started_items)
    committed_pts = done_pts + active_pts + not_started_pts

    # ── Velocity / daily rate ─────────────────────────────────────────────────
    past = await client.get_past_iterations(3)
    past_completed = [await client.get_completed_points(s.path) for s in past]
    avg_velocity: Optional[float] = (
        sum(past_completed) / len(past_completed) if past_completed else None
    )

    sprint_days = max(sprint.days_total, 1)
    days_elapsed = max(sprint.days_elapsed, 1)
    elapsed_frac = sprint.days_elapsed / sprint_days
    current_rate = done_pts / days_elapsed

    if avg_velocity is not None:
        hist_rate = avg_velocity / sprint_days
        if elapsed_frac < 0.25:
            effective_rate = hist_rate
            rate_basis = "historical (sprint too young for current-sprint data)"
        elif elapsed_frac < 0.60:
            effective_rate = 0.4 * hist_rate + 0.6 * current_rate
            rate_basis = "blended (40 % historical, 60 % current sprint)"
        else:
            effective_rate = current_rate
            rate_basis = "current sprint burn rate"
    else:
        effective_rate = current_rate
        rate_basis = "current sprint burn rate (no historical data)"

    remaining_days = sprint.days_remaining
    total_remaining_capacity = effective_rate * remaining_days

    # ── In-progress projection ────────────────────────────────────────────────
    stale_threshold = settings.at_risk_stale_days
    likely_active: list[dict] = []
    at_risk_active: list[dict] = []

    for item in active_items:
        days_in_state = item.days_in_current_state()
        entry = {
            "id": item.id,
            "title": item.title,
            "state": item.state,
            "story_points": item.story_points,
            "assigned_to": item.assigned_to,
            "days_in_state": days_in_state,
        }
        if days_in_state > stale_threshold:
            entry["projection"] = (
                f"At risk — stale in '{item.state}' for {days_in_state}d "
                f"(threshold {stale_threshold}d)"
            )
            at_risk_active.append(entry)
        else:
            entry["projection"] = "Likely to complete — has active momentum"
            likely_active.append(entry)

    active_likely_pts = sum(i.get("story_points") or 0 for i in likely_active)

    # ── Not-started projection ────────────────────────────────────────────────
    # Remaining capacity after in-progress items absorb their share
    capacity_for_new = max(0.0, total_remaining_capacity - active_likely_pts)

    # Prioritise: highest priority (lowest number) first, then smallest effort (quick wins)
    sorted_ns = sorted(
        not_started_items,
        key=lambda i: (i.priority or 99, i.story_points or 99),
    )

    likely_new: list[dict] = []
    unlikely_new: list[dict] = []
    cap = capacity_for_new

    for item in sorted_ns:
        pts = item.story_points or 0
        entry = {
            "id": item.id,
            "title": item.title,
            "state": item.state,
            "story_points": pts,
            "assigned_to": item.assigned_to,
            "priority": item.priority,
        }
        if pts == 0:
            entry["projection"] = "Unestimated — excluded from capacity model"
            unlikely_new.append(entry)
        elif cap >= pts:
            entry["projection"] = (
                f"Fits in remaining capacity ({cap:.1f} pts available after in-progress)"
            )
            likely_new.append(entry)
            cap -= pts
        else:
            entry["projection"] = (
                f"Insufficient capacity — needs {pts} pts, only {cap:.1f} remaining"
            )
            unlikely_new.append(entry)

    likely_new_pts = sum(i.get("story_points") or 0 for i in likely_new)

    # ── Totals ────────────────────────────────────────────────────────────────
    projected_pts = done_pts + active_likely_pts + likely_new_pts
    projected_items = len(done_items) + len(likely_active) + len(likely_new)

    if elapsed_frac >= 0.5 and len(past) >= 2:
        confidence = "high"
    elif elapsed_frac >= 0.25 or len(past) >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "sprint": sprint.name,
        "sprint_dates": sprint.to_dict(),
        "forecast": {
            "projected_complete_points": round(projected_pts, 1),
            "projected_complete_items": projected_items,
            "committed_points": round(committed_pts, 1),
            "committed_items": len(items),
            "gap_points": round(max(0.0, committed_pts - projected_pts), 1),
            "gap_items": max(0, len(items) - projected_items),
            "confidence": confidence,
        },
        "velocity": {
            "effective_daily_rate": round(effective_rate, 2),
            "rate_basis": rate_basis,
            "remaining_capacity_points": round(total_remaining_capacity, 1),
            "historical_avg_velocity": (
                round(avg_velocity, 1) if avg_velocity is not None else None
            ),
            "past_sprints_used": len(past),
        },
        "in_progress": {
            "total_count": len(active_items),
            "likely_complete": likely_active,
            "at_risk": at_risk_active,
        },
        "not_started": {
            "total_count": len(not_started_items),
            "capacity_available_points": round(capacity_for_new, 1),
            "likely_to_reach": likely_new,
            "unlikely_to_reach": unlikely_new,
        },
    }
