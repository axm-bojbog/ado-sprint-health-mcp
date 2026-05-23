"""propose_rebalance and execute_rebalance tool implementations.

Proposals are stored in a module-level dict with a 5-minute TTL.
This is intentionally simple: the server is single-instance and stateless
across restarts, which is fine for a demo and for the one-shot
propose → confirm → execute flow.
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

from ado.client import ADOClient
from config import settings

_PROPOSAL_TTL = 300  # seconds
_proposals: dict[str, dict] = {}

_DONE = frozenset({"Done", "Closed", "Resolved"})
_MOVABLE = frozenset({"New", "Ready", "Active", "In Progress", "Committed", "Approved", "Backlog"})


# ── Proposal store helpers ────────────────────────────────────────────────────

def _store(proposal: dict) -> str:
    _evict()
    pid = secrets.token_urlsafe(8)
    _proposals[pid] = {**proposal, "_expires_at": time.monotonic() + _PROPOSAL_TTL}
    return pid


def _fetch(pid: str) -> Optional[dict]:
    _evict()
    return _proposals.get(pid)


def _evict() -> None:
    now = time.monotonic()
    expired = [k for k, v in _proposals.items() if v["_expires_at"] < now]
    for k in expired:
        del _proposals[k]


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def propose_rebalance(
    from_user: str,
    to_user: str,
    max_items: int = 3,
    criteria: Optional[str] = None,
) -> dict:
    client = ADOClient()
    sprint = await client.get_current_iteration()
    items = await client.get_sprint_work_items(sprint.path)

    # Current committed (active, not done) points per person
    def active_pts(person: str) -> float:
        return sum(
            i.story_points or 0
            for i in items
            if i.assigned_to == person and i.state not in _DONE
        )

    from_pts = active_pts(from_user)
    to_pts = active_pts(to_user)

    # Team average for the cap check
    all_assigned = {i.assigned_to for i in items if i.assigned_to and i.state not in _DONE}
    total_pts = sum(active_pts(p) for p in all_assigned)
    team_avg = total_pts / max(len(all_assigned), 1)
    max_to_pts = team_avg * settings.overload_multiplier

    # Candidates: from_user's movable items, not done
    candidates = [
        i for i in items
        if i.assigned_to == from_user and i.state in _MOVABLE
    ]

    # Apply optional criteria filter
    if criteria:
        lc = criteria.lower()
        if "bug" in lc:
            candidates = [i for i in candidates if i.work_item_type.lower() == "bug"]
        elif "not started" in lc or "new" in lc:
            candidates = [i for i in candidates if i.state in ("New", "Ready", "Approved", "Backlog")]
        elif "small" in lc or "low effort" in lc:
            candidates = [i for i in candidates if (i.story_points or 0) <= 3]

    # Move smallest items first to minimise disruption; within same size, lower priority (higher number) first
    candidates.sort(key=lambda i: (i.story_points or 0, -(i.priority or 0)))

    proposed: list[dict] = []
    running_to_pts = to_pts

    for item in candidates:
        if len(proposed) >= max_items:
            break
        pts = item.story_points or 0
        if running_to_pts + pts > max_to_pts:
            continue
        proposed.append({
            "item_id": item.id,
            "title": item.title,
            "type": item.work_item_type,
            "state": item.state,
            "story_points": pts,
            "reason": (
                f"Moves {pts} pts from {from_user} "
                f"({from_pts} pts active) to {to_user} ({to_pts} pts active). "
                f"Team average: {team_avg:.0f} pts."
            ),
        })
        running_to_pts += pts

    if not proposed:
        return {
            "proposal_id": None,
            "message": (
                f"No suitable items found to move from '{from_user}' to '{to_user}'. "
                f"{from_user} has {from_pts} active pts; {to_user} has {to_pts} active pts; "
                f"team average is {team_avg:.0f} pts; cap for {to_user} is {max_to_pts:.0f} pts."
            ),
            "from_user_points": from_pts,
            "to_user_points": to_pts,
            "team_avg_points": round(team_avg, 1),
        }

    moved_pts = sum(m["story_points"] for m in proposed)
    proposal_data = {
        "sprint": sprint.name,
        "from_user": from_user,
        "to_user": to_user,
        "criteria": criteria,
        "proposed_moves": proposed,
        "impact": {
            "from_user_before": from_pts,
            "from_user_after": from_pts - moved_pts,
            "to_user_before": to_pts,
            "to_user_after": to_pts + moved_pts,
            "team_avg": round(team_avg, 1),
        },
    }
    pid = _store(proposal_data)

    return {
        "proposal_id": pid,
        "expires_in_seconds": _PROPOSAL_TTL,
        **proposal_data,
        "next_step": (
            f"Present this proposal to the user. "
            f"If they confirm, call execute_rebalance with proposal_id='{pid}'."
        ),
    }


async def execute_rebalance(proposal_id: str) -> dict:
    proposal = _fetch(proposal_id)
    if not proposal:
        return {
            "success": False,
            "error": (
                f"Proposal '{proposal_id}' was not found or has expired "
                f"(proposals expire after {_PROPOSAL_TTL // 60} minutes). "
                "Call propose_rebalance again to generate a new one."
            ),
        }

    client = ADOClient()
    moved: list[dict] = []
    failed: list[dict] = []
    to_user = proposal["to_user"]
    from_user = proposal["from_user"]
    pid_short = proposal_id[:6]

    for move in proposal["proposed_moves"]:
        iid = move["item_id"]
        audit = (
            f"[Sprint Health] Reassigned from {from_user} to {to_user} "
            f"as part of sprint rebalance (ref: {pid_short}). "
            f"Reason: {move['reason']}"
        )
        try:
            await client.reassign_work_item(iid, to_user, audit)
            moved.append({
                "item_id": iid,
                "title": move["title"],
                "story_points": move["story_points"],
            })
        except Exception as exc:
            failed.append({"item_id": iid, "title": move["title"], "error": str(exc)})

    # Consume the proposal regardless of partial failures
    _proposals.pop(proposal_id, None)

    return {
        "success": not failed,
        "proposal_id": proposal_id,
        "sprint": proposal["sprint"],
        "from_user": from_user,
        "to_user": to_user,
        "items_moved": len(moved),
        "items_failed": len(failed),
        "moved": moved,
        "failed": failed,
        "impact": proposal.get("impact", {}),
    }
