"""Writing one interview's copy into one Google Doc, as a tab of its own.

Franklin keeps "YOUTUBE LINKS FOR WEBSITE POSTING" with a tab per agent -
Lucia Ciemvado, Shareef Hijaz, Leonardo Lopez, Emmanuel Nazco - and the
segment copy is pasted into the tab by hand after RYTE writes it onto a Trello
card. This is that paste.

Narrow the way the inbox reader and the Drive uploader are narrow: the
document is set in .env and put into the request here rather than passed in,
so nothing calling this can write into another document. There is no delete
and no list of anybody's files - the only things in here are reading this
document's tabs and adding one.

Two requests rather than one, because a tab has to exist before anything can
be written into it and the id of a new tab only comes back once it is made.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .gsheets import Credentials, SheetsError, credentials, explain_token

API = "https://docs.googleapis.com/v1/documents"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: The scope this needs, which is not one the other Google work needed. Named
#: here because a 403 from Docs says nothing about which scope is missing and
#: the answer is always "mint the token again with this ticked".
SCOPE = "https://www.googleapis.com/auth/documents"

_DOC_IN_LINK = re.compile(r"/document/d/([A-Za-z0-9_-]{15,})")


class DocsError(RuntimeError):
    """Anything that stopped a read or a write, said in English."""


@dataclass
class Tab:
    """One tab of the document: enough to find it and to write into it."""

    tab_id: str
    title: str


def doc_id_in(link: str) -> str:
    """The document id out of a pasted link, or the id if that is all there is."""
    said = str(link or "").strip()
    found = _DOC_IN_LINK.search(said)
    if found:
        return found.group(1)
    return said if re.fullmatch(r"[A-Za-z0-9_-]{15,}", said) else ""


class DocsClient:
    """One signed-in session that can add a tab to one document."""

    def __init__(self, creds: Credentials, *, document: str, timeout: float = 30.0):
        if not (document or "").strip():
            raise DocsError(
                "No document set. SEGMENTS_DOC_ID in .env is the Google Doc "
                "the segment copy goes into - its link or its id - and it is "
                "the only document this can write to."
            )
        self._creds = creds
        self._document = document.strip()
        self._client = httpx.Client(timeout=timeout)
        self._token = ""
        self._token_until = 0.0

    def __enter__(self) -> "DocsClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _access_token(self) -> str:
        import time

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
            raise DocsError(f"Couldn't reach Google to sign in: {exc}") from exc
        if reply.status_code >= 400:
            raise DocsError(explain_token(reply.status_code, reply.text))
        got = reply.json()
        self._token = str(got.get("access_token") or "")
        self._token_until = time.time() + float(got.get("expires_in") or 3600) - 60
        if not self._token:
            raise DocsError("Google signed us in but sent no access token back.")
        return self._token

    def _call(self, method: str, path: str = "", **kwargs) -> dict:
        try:
            reply = self._client.request(
                method,
                f"{API}/{self._document}{path}",
                headers={"Authorization": f"Bearer {self._access_token()}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise DocsError(f"Couldn't reach Google Docs: {exc}") from exc

        if reply.status_code == 403:
            raise DocsError(
                "Google Docs refused that. The refresh token in .env was "
                f"minted without {SCOPE} - the Sheets and Drive scopes do not "
                "cover somebody else's document. Mint it again with that one "
                "ticked alongside the ones already there."
            )
        if reply.status_code == 404:
            raise DocsError(
                "Google has no document with that id, or the account the "
                "token belongs to cannot see it. Check SEGMENTS_DOC_ID in "
                ".env, and that the doc is shared with that account."
            )
        if reply.status_code >= 400:
            raise DocsError(f"Google Docs said {reply.status_code}: {reply.text[:200]}")
        return reply.json()

    def tabs(self) -> list[Tab]:
        """Every tab already in the document, so one is never made twice."""
        got = self._call(
            "GET", params={"includeTabsContent": "false"},
        )
        found = []
        for one in got.get("tabs") or []:
            found.extend(_tabs_in(one))
        return found

    def add_tab(self, title: str) -> Tab:
        """Make a tab with this title and hand back its id."""
        got = self._call(
            "POST", ":batchUpdate",
            json={"requests": [{"addDocumentTab": {"tabProperties": {"title": title}}}]},
        )
        made = (
            ((got.get("replies") or [{}])[0].get("addDocumentTab") or {})
            .get("tabProperties") or {}
        )
        tab_id = str(made.get("tabId") or "")
        if not tab_id:
            raise DocsError("Google made the tab but didn't say what its id is.")
        return Tab(tab_id=tab_id, title=str(made.get("title") or title))

    def write(self, tab: Tab, text: str) -> None:
        """Put the copy into that tab, at the end of whatever is in it."""
        self._call(
            "POST", ":batchUpdate",
            json={"requests": [{
                "insertText": {
                    "endOfSegmentLocation": {"tabId": tab.tab_id},
                    "text": text,
                }
            }]},
        )

    def link_to(self, tab: Tab) -> str:
        return f"https://docs.google.com/document/d/{self._document}/edit?tab={tab.tab_id}"


def _tabs_in(tab: dict) -> list[Tab]:
    """A tab and everything nested under it, flattened."""
    props = (tab or {}).get("tabProperties") or {}
    found = [Tab(tab_id=str(props.get("tabId") or ""), title=str(props.get("title") or ""))]
    for inside in (tab or {}).get("childTabs") or []:
        found.extend(_tabs_in(inside))
    return [one for one in found if one.tab_id]


def open_docs(secrets) -> DocsClient:
    """A signed-in client for the one document, or a sentence about what's missing.

    The same account and the same OAuth client as the rest of the Google work,
    with its own refresh token when there is one - the document lives in the
    same Workspace as the inbox the invoices arrive at.
    """
    try:
        creds = credentials(secrets)
    except SheetsError as exc:
        raise DocsError(str(exc).replace("Google Sheets", "Google Docs")) from exc

    instead = (getattr(secrets, "gmail_refresh_token", "") or "").strip()
    if instead:
        creds = Credentials(
            (getattr(secrets, "gmail_client_id", "") or "").strip() or creds.client_id,
            (getattr(secrets, "gmail_client_secret", "") or "").strip()
            or creds.client_secret,
            instead,
        )
    return DocsClient(
        creds, document=doc_id_in(getattr(secrets, "segments_doc_id", "") or "")
    )
