"""Sprint Health MCP server — tool registrations.

Import `mcp` from this module to get the FastMCP instance with all seven tools
attached. `main.py` wraps it in a Starlette ASGI app with auth middleware.
"""
from __future__ import annotations

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from config import settings
from tools.overview import get_sprint_overview as _overview
from tools.at_risk import find_at_risk_items as _at_risk
from tools.load import get_team_load as _load
from tools.blockers import summarize_blockers as _blockers
from tools.assign import reassign_work_item as _assign
from tools.rebalance import propose_rebalance as _propose, execute_rebalance as _execute
from tools.forecast import forecast_sprint_completion as _forecast
from tools.deferrals import propose_deferrals as _propose_defer, execute_deferrals as _execute_defer

mcp = FastMCP(
    settings.server_name,
    instructions=(
        "Sprint Health MCP encodes team-specific sprint judgment for an Azure DevOps project. "
        "Read tools: get_sprint_overview, find_at_risk_items, get_team_load, summarize_blockers. "
        "Forecast tool: forecast_sprint_completion — call it before AND after any write action "
        "to show the audience how the projected completion changes. "
        "Rebalance flow: propose_rebalance → (user confirms) → execute_rebalance. "
        "Deferral flow: propose_deferrals → (user confirms) → execute_deferrals. "
        "Single ad-hoc moves: reassign_work_item. "
        "ALWAYS present any proposal (rebalance or deferral) to the user and obtain explicit "
        "confirmation before calling the matching execute tool."
    ),
    # Disable DNS-rebinding protection: we run behind Azure App Service's reverse
    # proxy which rewrites the Host header to an internal hostname.  The MCP SDK
    # auto-enables protection (allowed_hosts uses ":*" port-wildcard patterns) only
    # when host="127.0.0.1", so it would reject every request with 421.
    # Azure's own network layer already provides perimeter protection.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _ok(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def _err(tool: str, exc: Exception) -> str:
    return json.dumps({"error": str(exc), "tool": tool}, indent=2)


# ── Read tools ────────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Return a structured summary of the current or a named sprint: "
        "work-item counts by state, committed vs completed effort, days remaining, "
        "rolling velocity from the last 3 sprints, and a calculated completion likelihood "
        "(on_track | at_risk | off_track)."
    )
)
async def get_sprint_overview(sprint_name: Optional[str] = None) -> str:
    """
    Args:
        sprint_name: Optional sprint name to query. Defaults to the current active sprint.
    """
    try:
        return _ok(await _overview(sprint_name))
    except Exception as exc:
        return _err("get_sprint_overview", exc)


@mcp.tool(
    description=(
        "Identify work items that are at risk of not completing this sprint. "
        "Applies four heuristics: (1) stale in an active state beyond the threshold, "
        "(2) assigned and active past sprint midpoint with no linked pull request, "
        "(3) high-effort items (>=8 pts) not started in the final third of the sprint, "
        "(4) items tagged 'Blocked'. Each flagged item includes its specific risk reasons."
    )
)
async def find_at_risk_items(sprint_name: Optional[str] = None) -> str:
    """
    Args:
        sprint_name: Optional sprint name to query. Defaults to the current active sprint.
    """
    try:
        return _ok(await _at_risk(sprint_name))
    except Exception as exc:
        return _err("find_at_risk_items", exc)


@mcp.tool(
    description=(
        "Return per-assignee workload metrics for the current or a named sprint: "
        "committed vs completed story points, item count, items in review, "
        "overdue item count, and a load classification "
        "(over_allocated | balanced | under_allocated) relative to the team average."
    )
)
async def get_team_load(sprint_name: Optional[str] = None) -> str:
    """
    Args:
        sprint_name: Optional sprint name to query. Defaults to the current active sprint.
    """
    try:
        return _ok(await _load(sprint_name))
    except Exception as exc:
        return _err("get_team_load", exc)


@mcp.tool(
    description=(
        "Return all blocked or stalled work items in the current or a named sprint, "
        "each with its most recent comment thread so the agent can explain why it is stuck."
    )
)
async def summarize_blockers(sprint_name: Optional[str] = None) -> str:
    """
    Args:
        sprint_name: Optional sprint name to query. Defaults to the current active sprint.
    """
    try:
        return _ok(await _blockers(sprint_name))
    except Exception as exc:
        return _err("summarize_blockers", exc)


# ── Write tools ───────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Move a single work item to a different assignee. "
        "An audit comment is added to the work-item history automatically. "
        "IMPORTANT: confirm the move with the user before calling this tool."
    )
)
async def reassign_work_item(
    item_id: int,
    new_assignee: str,
    comment: Optional[str] = None,
) -> str:
    """
    Args:
        item_id: The numeric Azure DevOps work-item ID.
        new_assignee: Display name or email of the new assignee.
        comment: Optional explanation added to the work-item history.
    """
    try:
        return _ok(await _assign(item_id, new_assignee, comment))
    except Exception as exc:
        return _err("reassign_work_item", exc)


@mcp.tool(
    description=(
        "Generate a rebalance proposal: a structured list of items to move from one team member "
        "to another, with reasoning per item. "
        "This tool DOES NOT execute anything — it returns a proposal_id that expires in 5 minutes. "
        "Present the proposal to the user for review and confirmation, then call execute_rebalance."
    )
)
async def propose_rebalance(
    from_user: str,
    to_user: str,
    max_items: Optional[int] = 3,
    criteria: Optional[str] = None,
) -> str:
    """
    Args:
        from_user: Display name of the over-allocated team member to move items from.
        to_user: Display name of the team member to receive the work.
        max_items: Maximum number of items to include in the proposal (default 3).
        criteria: Optional filter such as 'bugs only', 'not started', or 'small items'.
    """
    try:
        return _ok(await _propose(from_user, to_user, max_items or 3, criteria))
    except Exception as exc:
        return _err("propose_rebalance", exc)


@mcp.tool(
    description=(
        "Execute a previously generated rebalance proposal. "
        "Requires the proposal_id returned by propose_rebalance. "
        "Each reassigned item receives an audit comment. "
        "CRITICAL: Always obtain explicit user confirmation before calling this tool. "
        "Proposals expire 5 minutes after creation."
    )
)
async def execute_rebalance(proposal_id: str) -> str:
    """
    Args:
        proposal_id: The proposal_id returned by a prior propose_rebalance call.
    """
    try:
        return _ok(await _execute(proposal_id))
    except Exception as exc:
        return _err("execute_rebalance", exc)


# ── Forecast tool ─────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Project what the team will actually deliver by sprint end based on current state, "
        "recent velocity, and time remaining. "
        "Returns projected points and item count, split into: done, in-progress (likely vs at-risk), "
        "and not-started (likely-to-reach vs unlikely). "
        "Call this BEFORE and AFTER execute_rebalance or execute_deferrals to show the audience "
        "how the completion forecast improves — the delta is the key demo moment."
    )
)
async def forecast_sprint_completion(sprint_name: Optional[str] = None) -> str:
    """
    Args:
        sprint_name: Optional sprint name to forecast. Defaults to the current active sprint.
    """
    try:
        return _ok(await _forecast(sprint_name))
    except Exception as exc:
        return _err("forecast_sprint_completion", exc)


# ── Deferral tools ────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Generate a ranked list of not-started sprint items that are good candidates to defer "
        "to the next iteration, freeing at least target_points_to_remove story points. "
        "Each candidate includes its points, deferral reasoning, and dependency flag. "
        "Returns a proposal_id that expires in 5 minutes — does NOT move anything yet. "
        "Present the proposal to the user for review, then call execute_deferrals."
    )
)
async def propose_deferrals(
    target_points_to_remove: int,
    sprint_name: Optional[str] = None,
    criteria: Optional[str] = None,
) -> str:
    """
    Args:
        target_points_to_remove: Minimum story points to free from the sprint.
        sprint_name: Optional sprint name. Defaults to the current active sprint.
        criteria: Optional filter — e.g. 'bugs only', 'large items', 'unassigned'.
    """
    try:
        return _ok(await _propose_defer(target_points_to_remove, sprint_name, criteria))
    except Exception as exc:
        return _err("propose_deferrals", exc)


@mcp.tool(
    description=(
        "Execute a previously generated deferral proposal: move each candidate item to the next "
        "iteration (or a specified iteration) and post an audit comment on each item explaining "
        "the deferral. Requires the proposal_id returned by propose_deferrals. "
        "CRITICAL: Always obtain explicit user confirmation before calling this tool. "
        "Proposals expire 5 minutes after creation. "
        "After execution, call forecast_sprint_completion to show the updated projection."
    )
)
async def execute_deferrals(
    proposal_id: str,
    target_iteration: Optional[str] = None,
) -> str:
    """
    Args:
        proposal_id: The proposal_id returned by a prior propose_deferrals call.
        target_iteration: Optional sprint name to move items into. Defaults to the next
            scheduled iteration. Use 'Backlog' to move items to the project backlog.
    """
    try:
        return _ok(await _execute_defer(proposal_id, target_iteration))
    except Exception as exc:
        return _err("execute_deferrals", exc)
