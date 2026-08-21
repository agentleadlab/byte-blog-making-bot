"""Zoom cloud recordings, for reading a sales call that was posted in Discord.

The share link everyone passes around is a browser thing: it asks for a
passcode and hands back a player. The API is a different door - authenticated
as the account, no passcode involved - and it will hand over the transcript
Zoom generated when the call was recorded.

Two things have to be true on the Zoom side or there is nothing to fetch:

  * a Server-to-Server OAuth app exists, activated, with
    `cloud_recording:read:list_account_recordings:admin`
  * Settings -> Recording -> Advanced cloud recording -> **Audio transcript**
    is on, and *was already on when the call was recorded*. Zoom does not
    generate transcripts retroactively.

The second one is the trap: everything is configured correctly, the call is
found, and it simply has no transcript file because the setting was switched on
afterwards.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

BASE_URL = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"

# Zoom only accepts a month at a time, and a sales call is posted within days
# of happening - so this is the window worth searching, not a limit on history.
DEFAULT_LOOKBACK_DAYS = 30


class ZoomError(RuntimeError):
    """Raised when the Zoom API rejects a request."""


@dataclass
class ZoomRecording:
    """One recorded meeting, as much as is needed to name and read it."""

    topic: str
    share_url: str
    host_email: str = ""
    started_at: str = ""
    transcript_url: str = ""
    participants: tuple[str, ...] = ()

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript_url)


def share_key(url: str) -> str:
    """A share link reduced to the part that identifies the recording.

    Zoom hands out the same recording under several dressings - with `?pwd=`
    appended, with a trailing slash, sometimes with a different regional host -
    so comparing the raw strings misses matches that are obviously the same
    call. The token after the path is the identity.

    The path itself varies too, and that is not cosmetic: a browser link is
    `/rec/share/`, while the API answers with `/recording/share/`. Knowing only
    the first meant every link the API returned kept its whole URL as its
    "token" and matched nothing.
    """
    text = (url or "").strip()
    lowered = text.casefold()
    for marker in _SHARE_MARKERS:
        found = lowered.find(marker)
        if found >= 0:
            text = text[found + len(marker) :]
            break
    return text.split("?")[0].split("#")[0].strip("/").casefold()


# Longest first: `/recording/share/` contains no other marker, but checking a
# shorter one first would cut a URL at the wrong place.
_SHARE_MARKERS = (
    "/recording/share/",
    "/recording/play/",
    "/rec/share/",
    "/rec/play/",
)


def share_root(url_or_key: str) -> str:
    """The share token with its trailing segment dropped.

    Zoom share tokens come as `<token>.<suffix>`, and the suffix is not stable:
    the same recording handed out twice, or opened from a forwarded link, comes
    back with a different tail on the same token. Comparing whole strings calls
    those two different recordings when they are plainly the same one.
    """
    key = share_key(url_or_key) if "/" in (url_or_key or "") else (url_or_key or "").casefold()
    return key.split(".")[0]


def same_recording(one: str, other: str) -> bool:
    """Whether two share links point at the same recording.

    Exact first, then the token without its tail, then one containing the
    other - a link copied from the browser address bar is sometimes the token
    plus extra, and sometimes the token cut short.
    """
    left, right = share_key(one), share_key(other)
    if not left or not right:
        return False
    if left == right:
        return True

    left_root, right_root = share_root(left), share_root(right)
    # Guard the length: a short root would match half the account by accident.
    if len(left_root) >= 12 and left_root == right_root:
        return True
    if len(left_root) >= 12 and (left_root in right or right_root in left):
        return True
    return False


def pick_transcript(recording_files: list[dict]) -> str:
    """The download URL of the transcript, or "" when Zoom made none.

    Zoom returns every artifact of a recording together - video, audio, chat,
    the lot - and only one of them is the transcript. An empty answer here is
    the normal shape of "audio transcript was off when this was recorded",
    which is a different problem from a failed request.
    """
    for entry in recording_files or []:
        if str(entry.get("file_type") or "").upper() == "TRANSCRIPT":
            return str(entry.get("download_url") or "")
    return ""


def speakers(transcript: str) -> tuple[str, ...]:
    """Who spoke, in the order they first did.

    Zoom's transcript labels every turn with a display name, which is the only
    place a Zoom recording says who was actually on the call - the API gives a
    host email and a topic and nothing else. So the names for the card come
    from here.
    """
    found: list[str] = []
    for match in _SPEAKER_LINE.finditer(transcript or ""):
        name = " ".join(match.group("name").split())
        if name and name not in found:
            found.append(name)
    return tuple(found)


# "Santiago Villegas: hello" - a name, then a colon. Bounded to a handful of
# words so a sentence containing a colon isn't read as somebody's name.
_SPEAKER_LINE = re.compile(r"(?:^|\s)(?P<name>[A-Z][^:\n]{0,60}?):\s")


def name_from_email(email: str) -> str:
    """`santi@agentleadlab.com` -> `santi`. Only used to spot the host."""
    return (email or "").split("@")[0].replace(".", " ").replace("_", " ").strip().casefold()


def host_and_guests(transcript: str, host_email: str) -> tuple[str, tuple[str, ...]]:
    """(the closer, everyone else) from a transcript.

    The host is whoever's display name lines up with the account the recording
    is on. Everyone else was on the other side of the call.
    """
    people = speakers(transcript)
    if not people:
        return "", ()

    handle = name_from_email(host_email)
    host = ""
    if handle:
        parts = [part for part in handle.split() if part]
        for person in people:
            lowered = person.casefold()
            if any(part and part in lowered for part in parts):
                host = person
                break
    guests = tuple(person for person in people if person != host)
    return host, guests


def as_recording(meeting: dict) -> ZoomRecording:
    return ZoomRecording(
        topic=str(meeting.get("topic") or "").strip(),
        share_url=str(meeting.get("share_url") or ""),
        host_email=str(meeting.get("host_email") or ""),
        started_at=str(meeting.get("start_time") or ""),
        transcript_url=pick_transcript(meeting.get("recording_files") or []),
    )


def links_on(meeting: dict) -> list[str]:
    """Every URL a meeting carries that could be the one someone pasted.

    The meeting's own `share_url` is the usual answer, but a link copied from
    the player is a per-file URL, and those live on the recording files rather
    than the meeting. Both are worth comparing against before giving up.
    """
    found = [str((meeting or {}).get(field) or "") for field in ("share_url", "play_url")]
    for entry in (meeting or {}).get("recording_files") or []:
        if not isinstance(entry, dict):
            continue
        found.extend(str(entry.get(field) or "") for field in ("play_url", "download_url"))
    return [url for url in found if url]


def match_share_url(meetings: list[dict], share_url: str) -> ZoomRecording | None:
    """Find the meeting behind a link someone pasted into Discord."""
    if not share_key(share_url):
        return None
    for meeting in meetings or []:
        if any(same_recording(url, share_url) for url in links_on(meeting)):
            return as_recording(meeting)
    return None


def describe_match(meetings: list[dict], share_url: str, limit: int = 6) -> str:
    """Why a link didn't match, in terms of the strings actually compared.

    A link that doesn't match is exactly the moment the real shapes matter, and
    guessing at one cost three publish days once already. So this prints the
    token from the pasted link beside the tokens Zoom returned, rather than
    reporting "not found" and leaving it there.
    """
    wanted = share_key(share_url)
    lines = [
        f"Pasted link: `{(share_url or '')[:120]}`",
        f"Its token: `{wanted or '(none)'}`",
        "",
        "What Zoom returns for the newest recordings:",
    ]
    for meeting in (meetings or [])[:limit]:
        found = as_recording(meeting)
        lines.append(f"· **{found.topic or '(no topic)'}** {(found.started_at or '')[:10]}")
        urls = links_on(meeting)
        if not urls:
            lines.append("    (no links of any kind on this one)")
        for url in urls[:3]:
            lines.append(f"    `{url[:110]}`")
    if not meetings:
        lines.append("· (none)")

    # Which fields exist at all matters as much as their values: a share_url
    # that is simply absent from the account listing fails identically to one
    # that doesn't match, and only one of those is fixed by better matching.
    if meetings:
        sample = meetings[0]
        if isinstance(sample, dict):
            lines += ["", f"Fields on a meeting: {', '.join(sorted(sample))}"]
    return "\n".join(lines)


class ZoomClient:
    """Server-to-Server OAuth. The token lasts an hour and is fetched per run."""

    def __init__(self, account_id: str, client_id: str, client_secret: str, *, timeout: float = 60.0):
        self._account_id = account_id
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._client = httpx.Client(timeout=timeout)
        self._token: str | None = None

    def __enter__(self) -> "ZoomClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def token(self) -> str:
        if self._token:
            return self._token
        try:
            response = self._client.post(
                TOKEN_URL,
                params={"grant_type": "account_credentials", "account_id": self._account_id},
                headers={"Authorization": f"Basic {self._basic}"},
            )
        except httpx.HTTPError as exc:
            raise ZoomError(f"Couldn't reach Zoom for a token: {exc}") from exc

        if response.status_code >= 400:
            raise ZoomError(
                f"Zoom refused the credentials (HTTP {response.status_code}): "
                f"{response.text[:200]}. Check the app is *activated* in the "
                "marketplace - a built-but-inactive app fails exactly like a "
                "wrong secret."
            )
        self._token = str(response.json().get("access_token") or "")
        if not self._token:
            raise ZoomError("Zoom returned no access token.")
        return self._token

    def _get(self, path: str, **params) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{BASE_URL}{path}",
                headers={"Authorization": f"Bearer {self.token()}"},
                params=params,
            )
        except httpx.HTTPError as exc:
            raise ZoomError(f"GET {path} failed to send: {exc}") from exc
        if response.status_code >= 400:
            raise ZoomError(f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def account_recordings(self, *, days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
        """Recordings across the whole account, newest window first.

        Account-wide rather than per-user because the closers record under
        their own Zoom accounts, and asking each one in turn would need a user
        list and three times the requests.
        """
        today = date.today()
        meetings: list[dict] = []
        # Zoom caps a query at a month, so walk back in month-long windows.
        for start in range(0, max(days, 1), 30):
            window_end = today - timedelta(days=start)
            window_start = today - timedelta(days=min(start + 30, days))
            token = None
            while True:
                params = {
                    "from": window_start.isoformat(),
                    "to": window_end.isoformat(),
                    "page_size": 300,
                }
                if token:
                    params["next_page_token"] = token
                # `me` rather than the account id on purpose. Zoom reads a
                # literal account id as a master-account operation and demands
                # the `:master` scope; `me` is the same account through the
                # `:admin` scope that an ordinary Server-to-Server app is
                # granted.
                data = self._get("/accounts/me/recordings", **params)
                meetings.extend(data.get("meetings") or [])
                token = data.get("next_page_token")
                if not token:
                    break
        return meetings

    def find(self, share_url: str, *, days: int = DEFAULT_LOOKBACK_DAYS) -> ZoomRecording | None:
        return match_share_url(self.account_recordings(days=days), share_url)

    def transcript(self, recording: ZoomRecording) -> str:
        """Fetch the VTT transcript and return it as plain text."""
        if not recording.has_transcript:
            return ""
        try:
            response = self._client.get(
                recording.transcript_url,
                headers={"Authorization": f"Bearer {self.token()}"},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ZoomError(f"Couldn't download the transcript: {exc}") from exc
        if response.status_code >= 400:
            raise ZoomError(
                f"Transcript download -> HTTP {response.status_code}: {response.text[:200]}"
            )

        from .youtube import parse_captions

        return parse_captions(response.text)
