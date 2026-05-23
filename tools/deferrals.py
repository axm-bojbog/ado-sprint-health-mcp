"""propose_deferrals / execute_deferrals tool implementations.

Candidate selection
-------------------
Only not-started items (states: New, Ready, Approved, Backlog, etc.) are
eligible for deferral.  In-progress and done items are excluded — moving them
would disrupt active work.

Scoring / ordering
------------------
Candidates are sorted by:
  1. Story points descending — larger items free the most capacity quickly.
  2. Priority ascending (higher number = lower criticality) — prefer deferring
     less critical work first.

Greedy fill
-----------
Items are selected until pts_freed >= target_points_to_remove or candidates
are exhausted.  The proposal may slightly overshoot the target by one item.

Proposal cache
--------------
Identical TTL / fetch / evict pattern as rebalance.py.  Proposals expire in
5 minutes; execute_deferrals consumes and deletes the proposal on success.
"""
from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Optional

from ado.client import ADOClient

_NOT_STARTED = frozenset({"New", "Ready", "Approved", "Backlog", "Proposed"})

# In-memory store (process-local, matches rebalance.py pattern)
_proposals: dict[str, dict] = {}
_TTL = 300  # seconds


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _evict() -> None:
    now = time.monotonic()
    stale = [k for k, v in _proposals.items() if now - v["ts"] > _TTL]
    for k in stale:
        del _proposals[k]


def _store(data: dict) -> str:
    _evict()
    pid = str(uuid.uuid4())
    _proposals[pid] = {"ts": time.monotonic(), "data": data}
    return pid


def _fetch(pid: str) -> dict:
    _evict()
    entry = _proposals.get(pid)
    if not entry:
        raise ValueError(
            f"Proposal '{pid}' not found or expired (proposals expire in 5 minutes). "
            "Call propose_deferrals again to generate a fresh proposal."
        )
    return entry["data"]


# ── Tool implementations ───────────────────────────────────────────────────────

async def propose_deferrals(
    target_points_to_remove: int,
    sprint_name: Optional[str] = None,
    criteria: Optional[str] = None,
) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    # Fetch with relations so we can flag items that link to others
    items = await client.get_sprint_work_items(sprint.path, expand_relations=True)

    # Only not-started items are safe to defer
    candidates = [i for i in items if i.state in _NOT_STARTED]

    # Optional criteria filter
    crit = (criteria or "").lower()
    if "bug" in crit:
        candidates = [c for c in candidates if c.work_item_type.lower() == "bug"]
    elif "task" in crit:
        candidates = [c for c in candidates if c.work_item_type.lower() == "task"]
    elif "story" in crit or "user story" in crit:
        candidates = [c for c in candidates if c.work_item_type.lower() == "user story"]
    elif "large" in crit or "big" in crit:
        candidates = [c for c in candidates if (c.story_points or 0) >= 5]
    elif "small" in crit:
        candidates = [c for c in candidates if (c.story_points or 0) <= 3]
    elif "unassigned" in crit:
        candidates = [c for c in candidates if not c.assigned_to]

    # Sort: most capacity freed first, then least critical
    candidates.sort(key=lambda i: (-(i.story_points or 0), i.priority or 3))

    selected: list[dict] = []
    pts_freed = 0.0

    for item in candidates:
        if pts_freed >= target_points_to_remove:
            break

        pts = item.story_points or 0

        # Work-item relations (not Git/PR links) indicate dependency risk
        wi_rels = [
            r for r in item.relations
            if r.get("rel", "").startswith("System.LinkTypes")
        ]
        has_relations = bool(wi_rels)

        reasons: list[str] = [f"not yet started (state: '{item.state}')"]
        if not item.assigned_to:
            reasons.append("unassigned — no team member's workflow disrupted")
        if pts >= 8:
            reasons.append(f"large effort ({pts} pts) — maximum capacity relief per deferral")
        elif pts > 0:
            reasons.append(f"{pts} pts freed immediately")
        if not has_relations:
            reasons.append("no work-item dependencies detected")
        else:
            reasons.append(
                f"has {len(wi_rels)} linked relation(s) — review dependencies before deferring"
            )
        if (item.priority or 3) >= 3:
            reasons.append(f"priority {item.priority} — lower criticality makes deferral lower risk")

        selected.append({
            "id": item.id,
            "title": item.title,
            "work_item_type": item.work_item_type,
            "state": item.state,
            "story_points": pts,
            "assigned_to": item.assigned_to,
            "priority": item.priority,
            "has_relations": has_relations,
            "deferral_reasons": reasons,
        })
        pts_freed += pts

    # Resolve default destination: next scheduled iteration
    next_iter = await client.get_next_iteration()

    proposal_data = {
        "sprint_name": sprint.name,
        "items": selected,
        "next_iter_path": next_iter.path if next_iter else None,
        "next_iter_name": next_iter.name if next_iter else "Backlog",
    }
    proposal_id = _store(proposal_data)

    return {
        "proposal_id": proposal_id,
        "expires_in": "5 minutes",
        "sprint": sprint.name,
        "target_points_to_remove": target_points_to_remove,
        "actual_points_freed": round(pts_freed, 1),
        "items_proposed": len(selected),
        "default_target_iteration": next_iter.name if next_iter else "Backlog",
        "criteria_applied": criteria or "none",
        "candidates": selected,
        "next_step": (
            "Review the candidates above. Call execute_deferrals(proposal_id) to proceed, "
            "or add target_iteration='Sprint X' to override the default destination. "
            "You can also call forecast_sprint_completion before and after to show impact."
        ),
    }


async def execute_deferrals(
    proposal_id: str,
    target_iteration: Optional[str] = None,
) -> dict:
    data = _fetch(proposal_id)
    client = ADOClient()

    # Resolve destination iteration
    if target_iteration:
        target_sprint = await client.get_iteration_by_name(target_iteration)
        iter_path = target_sprint.path
        iter_name = target_sprint.name
    elif data["next_iter_path"]:
        iter_path = data["next_iter_path"]
        iter_name = data["next_iter_name"]
    else:
        # No next sprint found — move to project-level backlog
        iter_path = client.project
        iter_name = "Backlog"

    today = date.today().isoformat()
    deferred: list[dict] = []
    errors: list[dict] = []

    for item in data["items"]:
        comment = (
            f"Deferred from {data['sprint_name']} on {today} via Sprint Health MCP. "
            f"Moved to: {iter_name}. "
            "Reason: sprint capacity relief — item had not yet been started."
        )
        try:
            await client.move_work_item_to_iteration(item["id"], iter_path, comment=comment)
            deferred.append({
                "id": item["id"],
                "title": item["title"],
                "story_points": item["story_points"],
                "moved_to": iter_name,
            })
        except Exception as exc:
            errors.append({
                "id": item["id"],
                "title": item["title"],
                "error": str(exc),
            })

    # Consume proposal so it cannot be re-executed
    _proposals.pop(proposal_id, None)

    pts_freed = sum(r.get("story_points") or 0 for r in deferred)

    return {
        "sprint": data["sprint_name"],
        "target_iteration": iter_name,
        "deferred_count": len(deferred),
        "failed_count": len(errors),
        "points_freed": round(pts_freed, 1),
        "deferred_items": deferred,
        "errors": errors,
        "note": (
            "Items moved and audit comments posted. "
            "Call forecast_sprint_completion to see the updated completion projection."
        ),
    }
