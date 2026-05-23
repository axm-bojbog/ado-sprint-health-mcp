"""summarize_blockers tool implementation."""
from __future__ import annotations

import re
from typing import Optional

from ado.client import ADOClient
from config import settings

_DONE = frozenset({"Done", "Closed", "Resolved"})
_ACTIVE = frozenset({"Active", "In Progress", "Committed", "In Review"})

_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_RE.sub("", text).strip()


async def summarize_blockers(sprint_name: Optional[str] = None) -> dict:
    client = ADOClient()
    sprint = (
        await client.get_iteration_by_name(sprint_name)
        if sprint_name
        else await client.get_current_iteration()
    )

    items = await client.get_sprint_work_items(sprint.path)

    result: list[dict] = []
    for item in items:
        if item.state in _DONE:
            continue

        is_blocked = item.is_blocked()
        is_stale = (
            item.state in _ACTIVE
            and item.days_in_current_state() > settings.at_risk_stale_days
        )

        if not is_blocked and not is_stale:
            continue

        # Fetch the most recent comments
        raw_comments = await client.get_work_item_comments(item.id, top=5)
        comments: list[dict] = []
        for c in raw_comments[-3:]:  # at most last 3, ascending order
            author_raw = c.get("createdBy", {})
            author = (
                author_raw.get("displayName", "Unknown")
                if isinstance(author_raw, dict)
                else str(author_raw)
            )
            text = _strip_html(c.get("text", ""))[:400]
            if text:
                comments.append({"author": author, "text": text})

        reasons: list[str] = []
        if is_blocked:
            reasons.append("Tagged as 'Blocked'")
        if is_stale:
            days = item.days_in_current_state()
            reasons.append(
                f"Stale in '{item.state}' for {days} day{'s' if days != 1 else ''}"
            )

        result.append({
            **item.to_dict(),
            "block_reasons": reasons,
            "is_tagged_blocked": is_blocked,
            "is_stale": is_stale,
            "days_in_current_state": item.days_in_current_state(),
            "recent_comments": comments,
        })

    result.sort(key=lambda x: (-x["days_in_current_state"], x["title"]))

    return {
        "sprint": sprint.name,
        "blocked_or_stalled_count": len(result),
        "items": result,
    }
