"""Azure DevOps REST API client.

All public methods are async and create a fresh httpx.AsyncClient per call so
the module stays stateless and safe to import at module level.
"""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Optional

import httpx

from config import settings
from ado.models import Sprint, WorkItem

_API = "7.1"
_ITEM_TYPES = "'User Story', 'Bug', 'Feature', 'Task'"

# States that count as completed / done
DONE_STATES = frozenset({"Done", "Closed", "Resolved"})


def _basic_auth(pat: str) -> str:
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode()


def _build_headers(patch: bool = False) -> dict:
    ct = "application/json-patch+json" if patch else "application/json"
    return {
        "Authorization": _basic_auth(settings.ado_pat),
        "Content-Type": ct,
        "Accept": "application/json",
    }


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.rstrip("Z").split(".")[0]  # strip fractional seconds + Z
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.utcnow()


def _display_name(field_value) -> Optional[str]:
    """ADO returns AssignedTo as a dict or plain string depending on API version."""
    if isinstance(field_value, dict):
        return field_value.get("displayName") or field_value.get("uniqueName")
    return field_value or None


class ADOClient:
    def __init__(self) -> None:
        self.org = settings.ado_org_url
        self.project = settings.ado_project
        self.team = settings.ado_team

    # ── Iterations ────────────────────────────────────────────────────────────

    async def get_current_iteration(self) -> Sprint:
        url = f"{self.org}/{self.project}/{self.team}/_apis/work/teamsettings/iterations"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                url,
                headers=_build_headers(),
                params={"$timeframe": "current", "api-version": _API},
            )
            _raise(r)
            items = r.json().get("value", [])
        if not items:
            raise ValueError(
                "No current iteration found. "
                "Verify the team sprint is configured in Azure DevOps."
            )
        return _parse_iteration(items[0])

    async def get_iteration_by_name(self, name: str) -> Sprint:
        url = f"{self.org}/{self.project}/{self.team}/_apis/work/teamsettings/iterations"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=_build_headers(), params={"api-version": _API})
            _raise(r)
            items = r.json().get("value", [])
        for it in items:
            if it.get("name", "").lower() == name.lower():
                return _parse_iteration(it)
        raise ValueError(
            f"Sprint '{name}' not found. "
            f"Available: {[i['name'] for i in items]}"
        )

    async def get_past_iterations(self, count: int = 3) -> list[Sprint]:
        """Return the most recent `count` completed iterations.

        We fetch all iterations without a $timeframe filter because the ADO API
        returns 400 when $timeframe=past is used on some project configurations.
        We then filter client-side using the `timeFrame` attribute ADO returns on
        each iteration object ("past" | "current" | "future").
        """
        url = f"{self.org}/{self.project}/{self.team}/_apis/work/teamsettings/iterations"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                url,
                headers=_build_headers(),
                params={"api-version": _API},   # no $timeframe — filter below
            )
            _raise(r)
            items = r.json().get("value", [])
        past = [
            _parse_iteration(it)
            for it in items
            if it.get("attributes", {}).get("timeFrame", "").lower() == "past"
        ]
        past.sort(key=lambda s: s.finish_date, reverse=True)
        return past[:count]

    # ── Work items ────────────────────────────────────────────────────────────

    async def get_sprint_work_items(
        self,
        iteration_path: str,
        expand_relations: bool = False,
    ) -> list[WorkItem]:
        wiql = {
            "query": f"""
                SELECT [System.Id]
                FROM WorkItems
                WHERE [System.TeamProject] = '{self.project}'
                  AND [System.IterationPath] UNDER '{iteration_path}'
                  AND [System.WorkItemType] IN ({_ITEM_TYPES})
                  AND [System.State] <> 'Removed'
                ORDER BY [System.ChangedDate] DESC
            """
        }
        url = f"{self.org}/{self.project}/_apis/wit/wiql"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                url,
                headers=_build_headers(),
                json=wiql,
                params={"api-version": _API},
            )
            _raise(r)
            ids = [wi["id"] for wi in r.json().get("workItems", [])]
        if not ids:
            return []
        return await self.get_work_items_by_ids(ids, expand_relations=expand_relations)

    async def get_work_items_by_ids(
        self,
        ids: list[int],
        expand_relations: bool = False,
    ) -> list[WorkItem]:
        if not ids:
            return []
        expand = "all" if expand_relations else "fields"
        results: list[WorkItem] = []
        # ADO max 200 IDs per request
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(
                    f"{self.org}/_apis/wit/workitems",
                    headers=_build_headers(),
                    params={
                        "ids": ",".join(str(i) for i in chunk),
                        "$expand": expand,
                        "api-version": _API,
                    },
                )
                _raise(r)
                for raw in r.json().get("value", []):
                    results.append(_parse_work_item(raw))
        return results

    async def get_completed_points(self, iteration_path: str) -> float:
        """Sum story points of Done/Closed/Resolved items for velocity calculation."""
        wiql = {
            "query": f"""
                SELECT [System.Id]
                FROM WorkItems
                WHERE [System.TeamProject] = '{self.project}'
                  AND [System.IterationPath] = '{iteration_path}'
                  AND [System.WorkItemType] IN ({_ITEM_TYPES})
                  AND [System.State] IN ('Done', 'Closed', 'Resolved')
            """
        }
        url = f"{self.org}/{self.project}/_apis/wit/wiql"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                url,
                headers=_build_headers(),
                json=wiql,
                params={"api-version": _API},
            )
            _raise(r)
            ids = [wi["id"] for wi in r.json().get("workItems", [])]
        if not ids:
            return 0.0
        items = await self.get_work_items_by_ids(ids)
        return sum(i.story_points or 0.0 for i in items)

    async def get_work_item_comments(
        self, item_id: int, top: int = 3
    ) -> list[dict]:
        url = f"{self.org}/{self.project}/_apis/wit/workItems/{item_id}/comments"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                url,
                headers=_build_headers(),
                params={"$top": top, "order": "asc", "api-version": "7.1-preview.3"},
            )
            if r.status_code == 404:
                return []
            _raise(r)
            return r.json().get("comments", [])

    async def get_next_iteration(self) -> Optional[Sprint]:
        """Return the next upcoming iteration, or None if none is scheduled.

        Uses the same client-side filter as get_past_iterations — fetches all
        iterations and filters by attributes.timeFrame == 'future'.
        """
        url = f"{self.org}/{self.project}/{self.team}/_apis/work/teamsettings/iterations"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=_build_headers(), params={"api-version": _API})
            _raise(r)
            items = r.json().get("value", [])
        future = [
            _parse_iteration(it)
            for it in items
            if it.get("attributes", {}).get("timeFrame", "").lower() == "future"
        ]
        if not future:
            return None
        future.sort(key=lambda s: s.start_date)
        return future[0]

    # ── Mutations ─────────────────────────────────────────────────────────────

    async def move_work_item_to_iteration(
        self,
        item_id: int,
        iteration_path: str,
        comment: Optional[str] = None,
    ) -> dict:
        """Change a work item's IterationPath and optionally add a History comment."""
        patch: list[dict] = [
            {"op": "replace", "path": "/fields/System.IterationPath", "value": iteration_path}
        ]
        if comment:
            patch.append(
                {"op": "add", "path": "/fields/System.History", "value": comment}
            )
        url = f"{self.org}/{self.project}/_apis/wit/workitems/{item_id}"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(
                url,
                headers=_build_headers(patch=True),
                json=patch,
                params={"api-version": _API},
            )
            _raise(r)
            return r.json()

    async def reassign_work_item(
        self,
        item_id: int,
        new_assignee: str,
        comment: Optional[str] = None,
    ) -> dict:
        patch: list[dict] = [
            {"op": "replace", "path": "/fields/System.AssignedTo", "value": new_assignee}
        ]
        if comment:
            patch.append(
                {"op": "add", "path": "/fields/System.History", "value": comment}
            )
        url = f"{self.org}/{self.project}/_apis/wit/workitems/{item_id}"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(
                url,
                headers=_build_headers(patch=True),
                json=patch,
                params={"api-version": _API},
            )
            _raise(r)
            return r.json()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _raise(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.json().get("message", exc.response.text[:400])
        except Exception:
            body = exc.response.text[:400]
        raise RuntimeError(
            f"ADO API error {exc.response.status_code}: {body}"
        ) from exc


def _parse_iteration(raw: dict) -> Sprint:
    attrs = raw.get("attributes", {})
    start = _parse_date(attrs.get("startDate")) or datetime.utcnow()
    finish = _parse_date(attrs.get("finishDate")) or datetime.utcnow()
    return Sprint(
        id=raw["id"],
        name=raw["name"],
        path=raw["path"],
        start_date=start,
        finish_date=finish,
    )


def _parse_work_item(raw: dict) -> WorkItem:
    f = raw.get("fields", {})
    tags_raw = f.get("System.Tags") or ""
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]

    return WorkItem(
        id=raw["id"],
        title=f.get("System.Title", ""),
        work_item_type=f.get("System.WorkItemType", ""),
        state=f.get("System.State", ""),
        assigned_to=_display_name(f.get("System.AssignedTo")),
        story_points=f.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        iteration_path=f.get("System.IterationPath", ""),
        changed_date=_parse_date(f.get("System.ChangedDate")) or datetime.utcnow(),
        created_date=_parse_date(f.get("System.CreatedDate")) or datetime.utcnow(),
        state_change_date=_parse_date(f.get("Microsoft.VSTS.Common.StateChangeDate")),
        tags=tags,
        priority=f.get("Microsoft.VSTS.Common.Priority"),
        relations=raw.get("relations", []),
    )
