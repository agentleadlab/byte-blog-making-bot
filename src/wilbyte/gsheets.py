"""Google Sheets, for the one sheet RYTE keeps: the Levinson sales tracker.

Deliberately small. An earlier version of this file read and rewrote dozens of
lead masterlists, and it was taken out at the request of the person whose lead
sheets they were. What is here now reads a tab's headings and writes rows to
it. There is no clear, no delete, and no way to reach a spreadsheet other than
the one named in the config - not as a matter of intent but because the methods
do not exist.

Appending is the safe write. It cannot overwrite a row somebody typed, it
cannot reorder anything, and the worst case for a bug is a row too many at the
bottom, which anybody can delete.

`put` is the one exception and it is not safe in that way: it writes where it
is told and overwrites what is there. It exists because appending is Google
deciding where a row goes, and on the chargeback tracker - a list of disputes
in columns A-E and a deductions-by-closer summary from column G - Google chose
the summary and put the dispute underneath it. Whatever calls `put` is
responsible for having established that the range is empty.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://sheets.googleapis.com/v4/spreadsheets"

# Google's own advice for a user hitting the quota. Sheets allows 60 reads a
# minute per user, and a burst of four requests is nowhere near it - but a 429
# answered by trying again immediately is how a burst becomes a ban.
QUOTA_PAUSES = (2, 5, 15)


class SheetsError(RuntimeError):
    """Anything that stopped a read or a write, said in English."""


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str


def credentials(secrets) -> Credentials:
    missing = [
        name for name in ("google_client_id", "google_client_secret", "google_refresh_token")
        if not getattr(secrets, name, None)
    ]
    if missing:
        raise SheetsError(
            "Google Sheets isn't set up: "
            + ", ".join(name.upper() for name in missing)
            + " missing from .env."
        )
    return Credentials(
        client_id=secrets.google_client_id,
        client_secret=secrets.google_client_secret,
        refresh_token=secrets.google_refresh_token,
    )


def why_refused(body: str) -> str:
    """What Google actually said, out of the JSON it says it in.

    Worth the trouble because Google's 403s are not interchangeable and
    guessing between them costs somebody an afternoon: a missing scope and an
    API that was never enabled on the project look identical from the status
    code, and the message names which it is - and for a disabled API it
    carries the console link that turns it on.
    """
    import json

    try:
        said = json.loads(body or "{}")
    except (ValueError, TypeError):
        return " ".join((body or "").split())[:400]
    found = said.get("error")
    if isinstance(found, dict):
        return " ".join(str(found.get("message") or "").split())[:400]
    return " ".join(str(found or "").split())[:400]


def granted(creds: Credentials, *, timeout: float = 20.0) -> list[str]:
    """Which scopes this refresh token actually carries.

    Google says so on every refresh, in the `scope` field of the reply, and it
    is the only way to tell one refresh token from another: they all start
    "1//" and there is nothing in the value itself to read. Minting a new one
    and forgetting to paste it looks exactly like pasting it and forgetting to
    restart, and both look like the scope never having been ticked.

    Short names - "gmail.readonly", "documents" - because the full URLs are
    forty characters of prefix they all share.
    """
    try:
        reply = httpx.post(
            TOKEN_URL,
            timeout=timeout,
            data={
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except httpx.HTTPError as exc:
        raise SheetsError(f"Couldn't reach Google to sign in: {exc}") from exc
    if reply.status_code >= 400:
        raise SheetsError(explain_token(reply.status_code, reply.text))
    said = str((reply.json() or {}).get("scope") or "")
    return [one.rsplit("/", 1)[-1] for one in said.split() if one]


class SheetsClient:
    """One signed-in session. Reads headings, appends rows, nothing else."""

    def __init__(self, creds: Credentials, *, timeout: float = 30.0):
        self._creds = creds
        self._client = httpx.Client(timeout=timeout)
        self._token = ""
        self._token_until = 0.0

    def __enter__(self) -> "SheetsClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- signing in

    def _access_token(self) -> str:
        """A fresh access token, kept until a minute before it expires.

        The refresh token is the long-lived one and lives in .env. This is the
        hour-long one, and asking for a new one on every call would turn a
        four-request job into eight.
        """
        if self._token and time.time() < self._token_until:
            return self._token
        try:
            reply = self._client.post(
                TOKEN_URL,
                data={
                    "client_id": self._creds.client_id,
                    "client_secret": self._creds.client_secret,
                    "refresh_token": self._creds.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise SheetsError(f"Couldn't reach Google to sign in: {exc}") from exc

        if reply.status_code >= 400:
            raise SheetsError(explain_token(reply.status_code, reply.text))

        got = reply.json()
        self._token = str(got.get("access_token") or "")
        self._token_until = time.time() + float(got.get("expires_in") or 3600) - 60
        if not self._token:
            raise SheetsError("Google signed us in but sent no access token back.")
        return self._token

    def _request(self, method: str, path: str, **kwargs) -> dict:
        last = None
        for pause in (*QUOTA_PAUSES, None):
            token = self._access_token()
            try:
                reply = self._client.request(
                    method,
                    f"{API}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise SheetsError(f"{method} {path} failed to send: {exc}") from exc

            if reply.status_code == 429 and pause is not None:
                last = reply
                time.sleep(pause)
                continue
            if reply.status_code >= 400:
                raise SheetsError(explain(reply.status_code, reply.text))
            return reply.json() if reply.content else {}

        raise SheetsError(explain(429, last.text if last is not None else ""))

    # ------------------------------------------------------------------ reading

    def tabs(self, sheet_id: str) -> list[dict]:
        """Every tab in the spreadsheet: title, sheetId, and its size."""
        got = self._request(
            "GET",
            f"/{sheet_id}?fields=sheets.properties(sheetId,title,gridProperties)",
        )
        return [one.get("properties", {}) for one in got.get("sheets", [])]

    def rows(self, sheet_id: str, span: str) -> list[list[str]]:
        """The values in a range, as they are displayed."""
        got = self._request("GET", f"/{sheet_id}/values/{quoted(span)}")
        return [[str(cell) for cell in row] for row in got.get("values", [])]

    # ------------------------------------------------------------------ writing

    def tab_named(self, sheet_id: str, gid: str | int) -> str:
        """The title of the tab a `#gid=` in a link points at, or "".

        A sheet link carries the gid rather than the name - "…/edit?gid=
        1765528573" - and every write here is addressed by name. Resolving it
        means a link pasted out of the browser is enough, with nobody having
        to read the tab strip and type what it says.
        """
        wanted = str(gid).strip()
        if not wanted:
            return ""
        for one in self.tabs(sheet_id):
            found = one.get("properties") or one
            if str(found.get("sheetId")) == wanted:
                return str(found.get("title") or "")
        return ""

    def append(self, sheet_id: str, tab: str, rows: list[list[str]]) -> str:
        """Add rows under whatever is already in the tab. Returns their range.

        `INSERT_ROWS` rather than `OVERWRITE`: overwrite writes into the first
        empty row it finds, which on a sheet with a gap in it means writing
        over what comes after the gap.

        The range comes back because a row has to be formatted after it is
        written, and this is the only thing that knows where it landed.
        """
        if not rows:
            return ""
        got = self._request(
            "POST",
            f"/{sheet_id}/values/{quoted(tab)}:append"
            "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
            json={"values": rows},
        )
        return str((got.get("updates") or {}).get("updatedRange") or "")

    def put(self, sheet_id: str, span: str, rows: list[list[str]]) -> str:
        """Write rows into an exact range. Returns what was written to.

        `append` is told a tab and lets Google decide where the row goes, and
        on a tab with two tables side by side Google picks the wrong one. When
        the row belongs in a particular place, say the place.
        """
        if not rows:
            return ""
        got = self._request(
            "PUT",
            f"/{sheet_id}/values/{quoted(span)}?valueInputOption=USER_ENTERED",
            json={"values": rows},
        )
        return str(got.get("updatedRange") or "")

    def add_tab(self, sheet_id: str, title: str) -> int | None:
        """A new empty tab, and its id. None if one by that name already exists."""
        if any(one.get("title") == title for one in self.tabs(sheet_id)):
            return None
        got = self._request(
            "POST",
            f"/{sheet_id}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )
        made = (got.get("replies") or [{}])[0].get("addSheet") or {}
        found = (made.get("properties") or {}).get("sheetId")
        return int(found) if found is not None else None

    def match_row_above(
        self, sheet_id: str, tab_id: int, row: int, wide: int
    ) -> None:
        """Give a written row the look of the one above it.

        PASTE_FORMAT, so the values just written are untouched. It brings the
        font, the size, the borders, the number format and the dropdown - and
        a row that arrives in Google's default Calibri 11, with its date as
        left-aligned text, in a sheet somebody keeps in Arial 12, reads as an
        outsider's row before anybody has read what is in it.

        Never from row 1: the row above the first written one is the heading,
        and a dispute wearing the heading's clothes is the older bug that
        `restyle` exists for.
        """
        if tab_id is None or row < 3 or wide < 1:
            return
        self._request(
            "POST",
            f"/{sheet_id}:batchUpdate",
            json={"requests": [{
                "copyPaste": {
                    "source": {
                        "sheetId": tab_id,
                        "startRowIndex": row - 2, "endRowIndex": row - 1,
                        "startColumnIndex": 0, "endColumnIndex": wide,
                    },
                    "destination": {
                        "sheetId": tab_id,
                        "startRowIndex": row - 1, "endRowIndex": row,
                        "startColumnIndex": 0, "endColumnIndex": wide,
                    },
                    "pasteType": "PASTE_FORMAT",
                }
            }]},
        )

    def restyle(
        self, sheet_id: str, tab_id: int, first: int, last: int, *, bold: bool
    ) -> None:
        """Set bold and alignment on rows `first`..`last` (1-based, inclusive).

        A row appended to a tab holding nothing but its heading row arrives
        wearing the heading's clothes - bold and centred - because that is what
        Sheets copies down. The agents' names came out looking like column
        titles, which on a document going to an agency reads as a mistake
        before anybody has read a word of it.
        """
        if tab_id is None or last < first:
            return
        self._request(
            "POST",
            f"/{sheet_id}:batchUpdate",
            json={"requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": tab_id,
                        "startRowIndex": first - 1,
                        "endRowIndex": last,
                    },
                    "cell": {"userEnteredFormat": {
                        "horizontalAlignment": "LEFT",
                        "textFormat": {"bold": bold},
                    }},
                    "fields": (
                        "userEnteredFormat(horizontalAlignment,textFormat.bold)"
                    ),
                }
            }]},
        )


def quoted(span: str) -> str:
    return urllib.parse.quote(span, safe="")


def rows_in(span: str) -> tuple[int, int] | None:
    """The first and last row of a range like "August!A7:E10", or 'August 2026'!…"""
    found = re.findall(r"[A-Z]+(\d+)", (span or "").split("!")[-1])
    if not found:
        return None
    numbers = [int(one) for one in found]
    return min(numbers), max(numbers)


# The two halves of a sheet link: which spreadsheet, and which tab.
_SHEET_IN_LINK = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_GID_IN_LINK = re.compile(r"[?#&]gid=(\d+)")

# A Drive folder link, or the bare id somebody pasted instead.
_FOLDER_IN_LINK = re.compile(r"/folders/([A-Za-z0-9_-]{15,})")


def sheet_id_in(link: str) -> str:
    """The spreadsheet id out of a pasted link, or "" if there isn't one."""
    found = _SHEET_IN_LINK.search(str(link or ""))
    return found.group(1) if found else ""


def gid_in(link: str) -> str:
    """The tab's gid out of a pasted link, or ""."""
    found = _GID_IN_LINK.search(str(link or ""))
    return found.group(1) if found else ""


def folder_id_in(link: str) -> str:
    """The Drive folder id out of a pasted link, or the id if that is all
    somebody pasted."""
    said = str(link or "").strip()
    found = _FOLDER_IN_LINK.search(said)
    if found:
        return found.group(1)
    return said if re.fullmatch(r"[A-Za-z0-9_-]{15,}", said) else ""


def explain_token(status: int, body: str) -> str:
    """Why signing in failed, in terms of what to do about it."""
    said = _said(body)
    if "unauthorized_client" in said:
        # The token is real and the client is real; they are just not each
        # other's. Google says only "Unauthorized", which reads like a wrong
        # password and sends somebody back to mint the same token again.
        return (
            "That refresh token was minted for a different OAuth client than "
            "the GOOGLE_CLIENT_ID in .env. In the OAuth playground, tick the "
            "gear icon's **Use your own OAuth credentials** and paste this "
            "app's client ID and secret before authorizing - without it the "
            "token belongs to Google's own playground app. The client also "
            "needs https://developers.google.com/oauthplayground listed under "
            "Authorized redirect URIs."
        )
    if "invalid_grant" in said or status == 400:
        return (
            "Google rejected the refresh token. That happens when the OAuth "
            "app is back in Testing mode (tokens expire after 7 days), when "
            "the password on agentleadlab@gmail.com changed, or when access "
            "was revoked. Re-run the consent step and put the new "
            "GOOGLE_REFRESH_TOKEN in .env."
        )
    if status in (401, 403):
        return f"Google refused the sign-in ({status}). {said}"
    return f"Google wouldn't sign us in ({status}). {said}"


def explain(status: int, body: str) -> str:
    said = _said(body)
    if status == 403:
        return (
            "Google said no to that sheet (403). Either it isn't shared with "
            f"agentleadlab@gmail.com, or the token is missing the Sheets "
            f"scope. {said}"
        )
    if status == 404:
        return f"No sheet or tab by that name (404). {said}"
    if status == 429:
        return (
            "Google is rate-limiting us (429) and three waits didn't clear it. "
            "Try again in a minute."
        )
    return f"Google returned {status}. {said}"


def _said(body: str) -> str:
    """The message out of Google's error envelope, or the body as it came."""
    try:
        found = json.loads(body or "{}")
    except ValueError:
        return (body or "").strip()[:200]
    message = found.get("error")
    if isinstance(message, dict):
        return str(message.get("message") or "").strip()[:200]
    if isinstance(message, str):
        detail = found.get("error_description") or ""
        return f"{message} {detail}".strip()[:200]
    return (body or "").strip()[:200]
