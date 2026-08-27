"""Append finished segments to a Google Doc.

The last manual step: RYTE posts the clips in Discord, somebody selects them
and pastes them into the doc. This writes them straight in.

Appending only, never replacing. The doc is a working document with other
people's edits in it, and a write that could remove somebody's paragraph is
not worth the convenience of a tidier layout. Every insertion goes at the end,
and anything already in the doc is left exactly as it was.
"""

from __future__ import annotations

import os
import re

import httpx

from .segments import Segment

DOCS_ROOT = "https://docs.googleapis.com/v1/documents"

# Writing a document needs its own consent. The YouTube scope RYTE already has
# says nothing about Docs, so a refresh token minted for captions alone comes
# back 403 here - see `explain_scope` for what that looks like.
SCOPE = "https://www.googleapis.com/auth/documents"

# Where the segments go, unless a link says otherwise.
DOC_ID_VAR = "SEGMENTS_DOC_ID"

# A Google Docs URL: .../document/d/<id>/edit
_DOC_ID = re.compile(r"/document/d/([A-Za-z0-9_-]{20,})")


class DocsError(RuntimeError):
    """Raised when the doc can't be reached or written to."""


def doc_id(url_or_id: str) -> str:
    """The document id, from a pasted edit link or from the id itself."""
    text = (url_or_id or "").strip()
    found = _DOC_ID.search(text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    raise DocsError(f"That doesn't look like a Google Doc link: {text[:120]}")


def configured_doc() -> str:
    """The doc set in the environment, or "" when there isn't one."""
    return (os.getenv(DOC_ID_VAR) or "").strip()


def as_document(
    segments: list[Segment], *, heading: str = "", link: str = "", summary: str = ""
) -> str:
    """Everything to append, as one block of text.

    Built here rather than in the request so it can be read in a test and,
    more to the point, so the same text is what goes to Discord. Two renderers
    for one thing is how they drift.
    """
    parts: list[str] = []
    if heading:
        parts.append(heading)
    if link:
        parts.append(link)
    if summary:
        parts.append(summary)
    parts.extend(segment.as_text() for segment in segments)
    # A blank line between entries, and one at the end so the next run doesn't
    # start on the same line as this one's last word.
    return "\n\n".join(parts) + "\n\n"


def end_of(document: dict) -> int:
    """The index to insert at: just before the body's final newline.

    Docs keeps a newline at the end of every document that cannot be written
    after, so inserting at `endIndex` is refused outright. One before it is the
    end of the text as anybody reading the document would understand it.
    """
    content = ((document or {}).get("body") or {}).get("content") or []
    ends = [int(element.get("endIndex") or 0) for element in content]
    return max(max(ends, default=1) - 1, 1)


def _token() -> str:
    """An access token, from the same OAuth credentials the captions use."""
    from . import youtube_api

    if not youtube_api.oauth_credentials():
        missing = ", ".join(youtube_api.missing_oauth_vars())
        raise DocsError(
            f"Google OAuth isn't set up — {missing} still blank. I need it to "
            "write to the doc."
        )
    try:
        return youtube_api.access_token()
    except youtube_api.YouTubeAPIError as exc:
        raise DocsError(str(exc)) from exc


def append(
    segments: list[Segment],
    *,
    document: str = "",
    heading: str = "",
    link: str = "",
    summary: str = "",
) -> str:
    """Append the segments to the doc. Returns the doc's URL.

    Read first, then insert at the end of what was read. Docs has no "append"
    of its own - an insertion needs an index, and the only honest way to get
    the end of a document is to ask what is in it.
    """
    wanted = doc_id(document) if document else configured_doc()
    if not wanted:
        raise DocsError(
            f"No Google Doc set. Add {DOC_ID_VAR} to the .env, or paste the doc "
            "link with the command."
        )
    wanted = doc_id(wanted)

    text = as_document(segments, heading=heading, link=link, summary=summary)
    headers = {"Authorization": f"Bearer {_token()}"}

    try:
        read = httpx.get(
            f"{DOCS_ROOT}/{wanted}",
            headers=headers,
            params={"fields": "body.content.endIndex"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise DocsError(f"Couldn't reach Google Docs: {exc}") from exc
    if read.status_code >= 400:
        raise DocsError(explain(read, wanted))

    try:
        written = httpx.post(
            f"{DOCS_ROOT}/{wanted}:batchUpdate",
            headers=headers,
            json={"requests": [{
                "insertText": {"location": {"index": end_of(read.json())}, "text": text},
            }]},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise DocsError(f"Couldn't write to the doc: {exc}") from exc
    if written.status_code >= 400:
        raise DocsError(explain(written, wanted))

    return f"https://docs.google.com/document/d/{wanted}/edit"


def explain(response: httpx.Response, wanted: str) -> str:
    """Google's refusal in terms of what to do about it.

    A 403 here almost always means the refresh token was minted for captions
    and carries no Docs scope - which reads as "permission denied" and looks
    like a sharing problem, so somebody shares the doc again and it stays
    broken.
    """
    body = response.text[:300]
    if response.status_code == 403 and "insufficient" in body.lower():
        return (
            "The Google login RYTE has doesn't cover Docs. The refresh token was "
            "made for YouTube captions only, so it can read a video and not write "
            "a document. It needs minting again with both scopes: "
            f"`{SCOPE}` alongside the YouTube one."
        )
    if response.status_code == 403:
        return (
            "Google refused: the account behind GOOGLE_REFRESH_TOKEN doesn't have "
            f"edit access to that doc. Share it with that account, or check the "
            f"doc id is right. Google said: {body}"
        )
    if response.status_code == 404:
        return (
            f"No document with id `{wanted}`. Check the link — the id is the long "
            "string between /d/ and /edit."
        )
    return f"Google Docs -> HTTP {response.status_code}: {body}"
