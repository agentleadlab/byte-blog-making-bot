"""Notion client, for filing sales-call recordings into a gallery.

The recordings arrive in Discord as a link with the passcode on the line
underneath, and they need to end up in Notion looking like the rest of the
Agent Lead Lab pages - a gallery of cards, not a list of bare URLs.

A gallery view needs a *database*, so that is what this creates and writes to.
Credentials come from notion.so/my-integrations, and the target page has to be
shared with the integration (page -> ... -> Connections) or every call returns
404: Notion reports "not shared" and "does not exist" identically.
"""

from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"


class NotionError(RuntimeError):
    """Raised when the Notion API rejects a request."""


class NotionClient:
    def __init__(self, token: str, *, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": API_VERSION,
                "Content-Type": "application/json",
            },
        )

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NotionError(f"{method} {path} failed to send: {exc}") from exc

        if response.status_code == 404:
            raise NotionError(
                f"{method} {path} -> 404. Notion answers the same way for a page that "
                "doesn't exist and one that hasn't been shared with the integration - "
                "check the page's ... menu -> Connections."
            )
        if response.status_code >= 400:
            raise NotionError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    # ------------------------------------------------------------- reading

    def page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def query_database(self, database_id: str, *, page_size: int = 100) -> list[dict]:
        """Every row, following the cursor. Needed to work out the next number."""
        rows: list[dict] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{database_id}/query", json=body)
            rows.extend(data.get("results") or [])
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")
            if not cursor:
                return rows

    def find_child_database(self, page_id: str, title: str | None = None) -> str | None:
        """The database on the page to write into.

        With a title, the one that matches. Without, the first one there - the
        gallery already exists and is called whatever its owner called it
        ("‼️ Recordings ‼️"), so matching on a name we invented would quietly
        create a second one alongside it.
        """
        data = self._request("GET", f"/blocks/{page_id}/children", params={"page_size": 100})
        wanted = " ".join(title.split()).casefold() if title else None
        for block in data.get("results") or []:
            if block.get("type") != "child_database":
                continue
            if wanted is None:
                return block.get("id")
            found = (block.get("child_database") or {}).get("title") or ""
            if " ".join(found.split()).casefold() == wanted:
                return block.get("id")
        return None

    # ------------------------------------------------------------- writing

    def create_database(self, page_id: str, title: str, properties: dict) -> str:
        data = self._request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties,
            },
        )
        return str(data.get("id") or "")

    def add_columns(self, database_id: str, wanted: dict) -> list[str]:
        """Add any of `wanted` the database doesn't already have.

        The gallery was made by hand with Name, Created and Tags, so the link
        and passcode had nowhere to go. Adding a column is additive and
        reversible; silently dropping the two things a recording *is* would not
        be. Existing columns are matched by role rather than by exact name, so
        a "Recording URL" someone already made is used rather than duplicated.
        """
        existing = (self.database(database_id).get("properties") or {})
        have = {" ".join(name.split()).casefold() for name in existing}
        missing = {
            name: spec for name, spec in wanted.items()
            if " ".join(name.split()).casefold() not in have
        }
        if not missing:
            return []
        self._request("PATCH", f"/databases/{database_id}", json={"properties": missing})
        return sorted(missing)

    def create_page(
        self,
        database_id: str,
        properties: dict,
        *,
        children: list[dict] | None = None,
        cover_url: str | None = None,
        icon_url: str | None = None,
        icon_emoji: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            # Notion caps a create at 100 blocks; a call summary is nowhere near
            # that, and quietly dropping the rest would be worse than failing.
            body["children"] = children[:100]
        # External URLs only. Notion will not fetch and re-host, so a link that
        # expires - a Notion-hosted S3 URL, say - leaves every card blank later.
        if cover_url:
            body["cover"] = {"type": "external", "external": {"url": cover_url}}
        if icon_url:
            body["icon"] = {"type": "external", "external": {"url": icon_url}}
        elif icon_emoji:
            # An emoji is stored by Notion itself, so unlike a hosted image it
            # cannot rot, cost anything, or need setting up.
            body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        return self._request("POST", "/pages", json=body)


# ------------------------------------------------------------------ blocks


def heading(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _text(text)},
    }


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _text(text)}}


def bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _text(text)},
    }


def bookmark(url: str) -> dict:
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def _text(text: str) -> list[dict]:
    """Notion caps a single rich-text run at 2000 characters."""
    body = text or ""
    return [
        {"type": "text", "text": {"content": body[i : i + 2000]}}
        for i in range(0, max(len(body), 1), 2000)
    ]
