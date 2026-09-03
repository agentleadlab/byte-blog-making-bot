"""Trello REST client for the Agent Lead Lab daily board routine.

Replaces the manual moves and the evening rollover: four dated cards walk
In Que -> Today -> Quality Check each day, and whatever is still unticked at
the end has to land on tomorrow's card, on the right person's checklist.

The one thing that makes this worth automating rather than doing by hand: a
checklist item that links to another card is stored as *the card's URL*, and
Trello renders the name and status badge from that. Copying it by hand grabs
the rendered label instead, which produces dead text - the link is gone and the
badge stops updating. Copying the raw `name` field preserves it exactly.

Credentials come from https://trello.com/power-ups/admin (key, then token).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://api.trello.com/1"

# How long to wait before asking again when Trello has a bad second. Their API
# answers 503 now and then for no reason that outlasts the retry, and RYTE
# reads the board every twenty seconds all day - without this, a one-second
# outage in the middle of a rollover abandons it half done.
RETRY_PAUSES = (1.0, 3.0, 8.0)

# Worth asking again about. 429 is the rate limiter, which means the request
# was not performed at all; 5xx is their end having a moment.
_AGAIN = {429, 500, 502, 503, 504}

# Asking twice must not do it twice. GET changes nothing; PUT and DELETE say
# what the answer should be rather than "one more of these", so ticking an
# item complete twice leaves it complete. POST creates, and a POST that
# actually landed before the 503 would create a second card - those are asked
# again only when the rate limiter says it never ran.
_SAFE_TO_REPEAT = {"GET", "HEAD", "PUT", "DELETE"}


class TrelloError(RuntimeError):
    """Raised when the Trello API rejects a request."""


class TrelloClient:
    def __init__(self, key: str, token: str, *, timeout: float = 30.0):
        self._auth = {"key": key, "token": token}
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=timeout, headers={"Accept": "application/json"}
        )

    def __enter__(self) -> "TrelloClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- requests

    def _request(self, method: str, path: str, **kwargs) -> Any:
        params = {**self._auth, **kwargs.pop("params", {})}
        response = self._send(method, path, params, kwargs)

        if response.status_code >= 400:
            raise TrelloError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise TrelloError(f"{method} {path} returned non-JSON: {response.text[:200]}") from exc

    def _send(self, method: str, path: str, params: dict, kwargs: dict):
        """The request, asked again when the answer was Trello having a moment.

        Returns the last response whatever it says - a 503 that never clears is
        still a 503, and the caller raises it with the status in the message.
        """
        for pause in (*RETRY_PAUSES, None):
            try:
                response = self._client.request(method, path, params=params, **kwargs)
            except httpx.HTTPError as exc:
                # A request that failed to send never reached Trello, so asking
                # again cannot duplicate anything, whatever the method was.
                if pause is None:
                    raise TrelloError(f"{method} {path} failed to send: {exc}") from exc
                self._wait(pause)
                continue

            if pause is None or response.status_code not in _AGAIN:
                return response
            repeatable = (
                method.upper() in _SAFE_TO_REPEAT or response.status_code == 429
            )
            if not repeatable:
                return response
            self._wait(pause)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _wait(seconds: float) -> None:
        time.sleep(seconds)

    # ------------------------------------------------------------- reading

    def board_lists(self, board_id: str) -> list[dict]:
        """Every list on the board, in board order."""
        return self._request("GET", f"/boards/{board_id}/lists", params={"cards": "none"})

    def list_cards(self, list_id: str) -> list[dict]:
        return self._request(
            "GET", f"/lists/{list_id}/cards",
            # dueComplete is the green tick on the card front. Trello reuses
            # the due-date field for it, and it is set on cards that have no
            # due date at all - which is all of these.
            params={
                # shortUrl as well as url: a checklist item stores whichever
                # was copied, and looking an agent's name up by the wrong one
                # silently finds nothing.
                "fields": "name,idList,url,shortUrl,dateLastActivity,dueComplete",
            },
        )

    def card_checklists(self, card_id: str) -> list[dict]:
        """Checklists on a card, each with its items.

        `checkItems` come back with `name` and `state` ('complete'/'incomplete'),
        and `name` is the raw text - a URL when the item links to another card.
        """
        return self._request(
            "GET",
            f"/cards/{card_id}/checklists",
            # `id` on the items because removing one needs it - see
            # `remove_check_item`. Trello omits it unless it is asked for.
            params={"checkItems": "all", "checkItem_fields": "id,name,state,pos"},
        )

    def remove_check_item(self, checklist_id: str, item_id: str) -> dict:
        """Take one line off a checklist.

        The only delete in this codebase, and it is here because the
        alternative was somebody hand-removing thirty lines they could not
        identify. Scoped to items RYTE wrote and can write again.
        """
        return self._request(
            "DELETE", f"/checklists/{checklist_id}/checkItems/{item_id}"
        )

    def card(self, card_id: str) -> dict:
        return self._request("GET", f"/cards/{card_id}", params={"fields": "name,idList,url"})

    def card_detail(self, card_id: str) -> dict:
        """A card with its description and short URL, which the list view omits."""
        return self._request(
            "GET", f"/cards/{card_id}",
            params={"fields": "name,idList,url,shortUrl,desc"},
        )

    def add_comment(self, card_id: str, text: str) -> dict:
        """Say something on a card, as whoever the token belongs to."""
        return self._request(
            "POST", f"/cards/{card_id}/actions/comments", params={"text": text}
        )

    def card_comments(self, card_id: str) -> list[str]:
        """What people have said on a card, newest first.

        The launch date turns up in a comment about as often as in the
        description - Faith and Casey add it after the form has already made
        the card - so both get read and neither is the official one.
        """
        actions = self._request(
            "GET", f"/cards/{card_id}/actions",
            params={"filter": "commentCard", "limit": 50},
        )
        return [
            str((item.get("data") or {}).get("text") or "")
            for item in actions or []
        ]

    # ------------------------------------------------------------- writing

    def move_card(self, card_id: str, list_id: str, *, position: str = "top") -> dict:
        """Move a card to another list, at the top of it by default.

        Trello drops a moved card at the bottom unless told otherwise, and the
        bottom of Done is under forty-nine other cards. `pos` takes "top",
        "bottom", or a number.
        """
        return self._request(
            "PUT", f"/cards/{card_id}", params={"idList": list_id, "pos": position}
        )

    def set_description(self, card_id: str, text: str) -> dict:
        """Replace a card's description.

        Replaces - there is no append in the API. Read `card_detail` first and
        send the old text back with the new part on the end, or somebody's
        notes go missing.
        """
        return self._request("PUT", f"/cards/{card_id}", params={"desc": text})

    def archive_card(self, card_id: str) -> dict:
        """Archive a card. Reversible - Trello keeps it and it can come back.

        Deliberately not the DELETE endpoint. Nothing in this codebase deletes
        a card, because a wrong archive is an afternoon and a wrong delete is
        gone.
        """
        return self._request("PUT", f"/cards/{card_id}", params={"closed": "true"})

    def create_card(self, list_id: str, name: str, *, position: str = "top") -> dict:
        return self._request(
            "POST", "/cards", params={"idList": list_id, "name": name, "pos": position}
        )

    def create_checklist(self, card_id: str, name: str) -> dict:
        return self._request("POST", "/checklists", params={"idCard": card_id, "name": name})

    def add_check_item(self, checklist_id: str, name: str, *, checked: bool = False) -> dict:
        """Add an item, sending the name exactly as it was read.

        Verbatim on purpose: when the item is a linked card the name *is* the
        URL, and anything that tidies or shortens it breaks the link.
        """
        return self._request(
            "POST",
            f"/checklists/{checklist_id}/checkItems",
            params={"name": name, "checked": "true" if checked else "false"},
        )


# ------------------------------------------------------------------ helpers


def find_list(lists: list[dict], name: str) -> dict | None:
    """A list by name, case- and whitespace-insensitively.

    Board list names carry emoji and stray spaces, and one of them is spelled
    "Pendng A2P" - matching has to be forgiving or the routine breaks on a typo
    nobody wants to fix.
    """
    target = " ".join(name.split()).casefold()
    for item in lists:
        if " ".join(str(item.get("name", "")).split()).casefold() == target:
            return item
    return None


def item_is_link(name: str) -> bool:
    """True when a checklist item carries a Trello link rather than plain text."""
    return "trello.com/c/" in (name or "")


def linked_card_id(name: str) -> str | None:
    """The card short-id inside a linked checklist item, if there is one."""
    import re

    match = re.search(r"trello\.com/c/([A-Za-z0-9]+)", name or "")
    return match.group(1) if match else None
