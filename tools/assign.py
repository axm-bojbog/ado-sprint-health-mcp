"""reassign_work_item tool implementation."""
from __future__ import annotations

from typing import Optional

from ado.client import ADOClient


async def reassign_work_item(
    item_id: int,
    new_assignee: str,
    comment: Optional[str] = None,
) -> dict:
    client = ADOClient()

    items = await client.get_work_items_by_ids([item_id])
    if not items:
        return {"success": False, "error": f"Work item {item_id} not found."}

    item = items[0]
    previous = item.assigned_to or "unassigned"
    audit = (
        comment
        or f"Reassigned from {previous} to {new_assignee} via Sprint Health MCP."
    )

    await client.reassign_work_item(item_id, new_assignee, audit)

    return {
        "success": True,
        "item_id": item_id,
        "title": item.title,
        "type": item.work_item_type,
        "state": item.state,
        "previous_assignee": previous,
        "new_assignee": new_assignee,
        "audit_comment": audit,
    }
