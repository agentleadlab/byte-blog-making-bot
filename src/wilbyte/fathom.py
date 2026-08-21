"""Fathom, for the closer whose calls it records.

Fathom sits in the meeting as a notetaker, so unlike Zoom there is no passcode
and no separate transcription setting to have forgotten - if the call is in
Fathom at all, its transcript exists. That makes it the better source where
both are available.

Deliberately tolerant about the shape of what comes back. This integration was
written without a key in hand to try it against, and the last time a response
shape was assumed rather than checked it cost three missed publish days. So
every field is looked for under the names it plausibly carries, a transcript is
accepted as a string or as a list of speaker turns, and `describe` exists to
print what actually arrived when something doesn't match.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

BASE_URL = "https://api.fathom.ai/external/v1"


class FathomError(RuntimeError):
    """Raised when the Fathom API rejects a request."""


# Where a value might live, in the order worth trying.
TITLE_FIELDS = ("title", "meeting_title", "name", "topic")
URL_FIELDS = ("url", "share_url", "meeting_url", "recording_url", "share_link", "permalink")
TRANSCRIPT_FIELDS = ("transcript", "transcript_text", "segments", "transcript_segments")
SPEAKER_FIELDS = ("speaker", "speaker_name", "display_name", "name")


def first_of(data: dict, fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = (data or {}).get(field)
        if value:
            return value
    return None


def share_key(url: str) -> str:
    """A Fathom link reduced to the id that identifies the call.

    Fathom links appear as /share/<token> and /calls/<id>, and the same call
    gets passed round in both forms plus whatever tracking query someone's
    email client bolted on.
    """
    text = (url or "").strip()
    lowered = text.casefold()
    for marker in ("/share/", "/calls/", "/call/"):
        found = lowered.find(marker)
        if found >= 0:
            text = text[found + len(marker) :]
            break
    return text.split("?")[0].split("#")[0].strip("/").casefold()


@dataclass
class FathomCall:
    title: str
    url: str
    started_at: str = ""
    participants: tuple[str, ...] = ()
    recorded_by: str = ""
    guests: tuple[str, ...] = ()
    summary: str = ""
    raw: dict | None = None


def recorded_by(meeting: dict) -> str:
    """Whose Fathom recorded the call - the closer, for naming the card."""
    value = (meeting or {}).get("recorded_by")
    if isinstance(value, dict):
        return str(first_of(value, SPEAKER_FIELDS) or value.get("email") or "").strip()
    return str(value or "").strip()


def guests(meeting: dict) -> tuple[str, ...]:
    """Everyone on the call who isn't from Agent Lead Lab.

    Fathom labels each invitee internal or external, which is what separates
    the agent from the closer without having to know either name in advance.
    """
    found: list[str] = []
    for entry in (meeting or {}).get("calendar_invitees") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("is_external") is False:
            continue
        kind = str(entry.get("domain_type") or entry.get("type") or "").casefold()
        if kind and "internal" in kind:
            continue
        name = str(first_of(entry, SPEAKER_FIELDS) or entry.get("email") or "").strip()
        if name and name not in found:
            found.append(name)
    return tuple(found)


def as_call(meeting: dict) -> FathomCall:
    return FathomCall(
        recorded_by=recorded_by(meeting),
        guests=guests(meeting),
        # Fathom writes its own summary. Using it costs nothing and describes
        # the call in the platform's own terms; ours is the fallback.
        summary=str(first_of(meeting, ("default_summary", "summary")) or "").strip(),
        title=str(first_of(meeting, TITLE_FIELDS) or "").strip(),
        url=str(first_of(meeting, URL_FIELDS) or ""),
        started_at=str(
            first_of(meeting, ("recording_start_time", "scheduled_start_time", "created_at")) or ""
        ),
        participants=participants(meeting),
        raw=meeting,
    )


def participants(meeting: dict) -> tuple[str, ...]:
    """Who was on the call, for naming the card.

    Fathom carries these under several keys depending on how the meeting was
    created, and each entry is sometimes a string and sometimes an object.
    """
    found: list[str] = []
    for key in (
        "calendar_invitees", "invitees", "participants", "attendees", "external_invitees"
    ):
        for entry in (meeting or {}).get(key) or []:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, dict):
                name = str(first_of(entry, SPEAKER_FIELDS) or entry.get("email") or "")
            else:
                continue
            name = name.strip()
            if name and name not in found:
                found.append(name)
    return tuple(found)


def transcript_text(meeting: dict) -> str:
    """The transcript as plain text, whichever shape it arrived in.

    A string comes back as-is. A list of speaker turns is flattened to
    "Name: what they said", which is what makes a sales-call summary able to
    say who raised the objection rather than just that one was raised.
    """
    value = first_of(meeting, TRANSCRIPT_FIELDS)
    if isinstance(value, str):
        return value.strip()

    lines: list[str] = []
    for turn in value or []:
        if isinstance(turn, str):
            lines.append(turn.strip())
            continue
        if not isinstance(turn, dict):
            continue
        said = str(turn.get("text") or turn.get("transcript") or "").strip()
        if not said:
            continue
        speaker = turn.get("speaker")
        if isinstance(speaker, dict):
            speaker = first_of(speaker, SPEAKER_FIELDS)
        speaker = str(speaker or first_of(turn, SPEAKER_FIELDS) or "").strip()
        lines.append(f"{speaker}: {said}" if speaker else said)
    return "\n".join(lines).strip()


def match_share_url(meetings: list[dict], share_url: str) -> FathomCall | None:
    wanted = share_key(share_url)
    if not wanted:
        return None
    for meeting in meetings or []:
        for field in URL_FIELDS:
            if share_key(str((meeting or {}).get(field) or "")) == wanted:
                return as_call(meeting)
    return None


def _retry_after(response, *, default: float) -> float:
    """How long Fathom asked us to wait, capped so nothing hangs on a bad header."""
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, min(float(raw), 30.0))
    except (TypeError, ValueError):
        return default


def meeting_id(meeting: dict) -> str:
    """A stable identity for one Fathom call."""
    for field in ("id", "recording_id", "meeting_id", "uuid"):
        value = (meeting or {}).get(field)
        if value:
            return str(value)
    return share_key(str(first_of(meeting, URL_FIELDS) or ""))


def choose(
    meetings: list[dict],
    *,
    link: str = "",
    filed: set[str] | frozenset[str] | None = None,
) -> tuple[FathomCall | None, str]:
    """Which call was posted, and how that was decided."""
    found = match_share_url(meetings, link)
    if found is not None:
        return found, "the link"
    # No recency fallback here either. Fathom's links do resolve, so this path
    # means something is genuinely wrong - and the Zoom version of this guess
    # filed two cards carrying a summary of a different client's call.
    return None, ""


def describe(meetings: list[dict], limit: int = 3) -> str:
    """What Fathom actually returned, for when a link doesn't match.

    Guessing at a response shape twice is a habit worth breaking: this puts
    the real field names in front of whoever is reading the error.
    """
    if not meetings:
        return "Fathom returned no calls at all for that window."
    sample = meetings[0]
    keys = ", ".join(sorted(sample)) if isinstance(sample, dict) else type(sample).__name__
    titles = [str(first_of(m, TITLE_FIELDS) or "(untitled)") for m in meetings[:limit]]
    return (
        f"{len(meetings)} call(s) visible. Fields on one: {keys}. "
        f"Most recent: {'; '.join(titles)}"
    )


class FathomClient:
    def __init__(self, api_key: str, *, timeout: float = 60.0):
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def __enter__(self) -> "FathomClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, *, attempts: int = 3, **params) -> dict[str, Any]:
        for attempt in range(attempts):
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise FathomError(f"GET {path} failed to send: {exc}") from exc

            if response.status_code == 401:
                raise FathomError(
                    "Fathom rejected the API key. Check it was copied whole from "
                    "fathom.video -> Settings, and that the plan includes API access."
                )
            # Rate limited. Fathom's allowance is small, and waiting is the
            # whole remedy - so wait rather than surfacing a failure that only
            # means "too fast".
            if response.status_code == 429 and attempt < attempts - 1:
                time.sleep(_retry_after(response, default=2.0 * (attempt + 1)))
                continue
            if response.status_code == 429:
                raise FathomError(
                    "Fathom is rate-limiting us. Give it a minute and post the "
                    "link again — nothing is wrong with the key or the call."
                )
            if response.status_code >= 400:
                raise FathomError(
                    f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}"
                )
            return response.json() if response.content else {}
        return {}

    # Enough to cover the calls of the last fortnight without walking the whole
    # history. Every page is a request against a small allowance, and asking
    # for two hundred calls to identify one is what earned the 429.
    DEFAULT_LIMIT = 25
    MAX_PAGES = 4

    def meetings(self, *, include_transcript: bool = False, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Recent calls, newest first, following the cursor a few pages at most.

        Transcripts are left out by default. Fathom writes its own summary of
        every call and that is what ends up on the card, so pulling the full
        text of two hundred calls was fetching the one thing nobody reads - at
        the cost of the rate limit that took RYTE off the air.
        """
        found: list[dict] = []
        cursor = None
        for _ in range(self.MAX_PAGES):
            if len(found) >= limit:
                break
            params: dict[str, Any] = {}
            if include_transcript:
                params["include_transcript"] = "true"
            if cursor:
                params["cursor"] = cursor
            data = self._get("/meetings", **params)
            batch = data.get("items") or data.get("meetings") or data.get("data") or []
            found.extend(batch)
            cursor = data.get("next_cursor") or data.get("cursor")
            if not cursor or not batch:
                break
        return found[:limit]

    def transcript_for(self, wanted_id: str) -> str:
        """The full transcript of one call, fetched only when it's needed.

        Which is rarely: Fathom writes a summary of every call and that is what
        goes on the card. This is the path for a call it hasn't summarised.
        """
        if not wanted_id:
            return ""
        for meeting in self.meetings(include_transcript=True):
            if meeting_id(meeting) == wanted_id:
                return transcript_text(meeting)
        return ""

    def find(self, share_url: str) -> tuple[FathomCall | None, list[dict]]:
        """The call behind a posted link, and everything seen while looking.

        The second value is what `describe` reports on. A link that doesn't
        match is the moment the response shape matters, so the evidence is
        handed back rather than thrown away.
        """
        meetings = self.meetings()
        return match_share_url(meetings, share_url), meetings
