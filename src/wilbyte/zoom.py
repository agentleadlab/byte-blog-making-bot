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
    # Zoom's own identity for the meeting, so a call filed once isn't offered
    # again when the next link can't be matched.
    uid: str = ""

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


def speakers(vtt: str) -> tuple[str, ...]:
    """Who spoke, in the order they first did.

    Read from the *raw* VTT, one cue line at a time. Zoom writes each cue as
    "Display Name: what they said", and a name only means anything at the start
    of its own line. Run over the flattened prose instead and the pattern walks
    straight through a sentence: "Santiago Villegas Agent Lead Lab: Derrick,
    what's going on? How are you? Good morning. Derrick Robison: Hey man" gives
    up "How are you? Good morning. Derrick Robison" as somebody's name, which is
    exactly what ended up on a card.
    """
    found: list[str] = []
    for line in (vtt or "").splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        match = _CUE_SPEAKER.match(line)
        if not match:
            continue
        name = " ".join(match.group("name").split())
        if _reads_as_a_name(name) and name not in found:
            found.append(name)
    return tuple(found)


# A cue line opening with "Name: ". Anchored to the start of the line, and the
# name may not run past a sentence boundary.
_CUE_SPEAKER = re.compile(r"^(?P<name>[^:]{1,48}):\s+\S")

MAX_NAME_WORDS = 6


# Words that sit inside a real name without being capitalised.
_NAME_CONNECTORS = {"of", "and", "the", "de", "del", "la", "van", "von", "da", "di"}


def _reads_as_a_name(text: str) -> bool:
    """Whether a cue's label is plausibly somebody's display name.

    Zoom names carry company suffixes and job titles, so word count alone can't
    decide it. What does decide it is capitalisation: a display name is
    capitalised throughout, and "So here's the thing: it worked" is not - which
    is the shape of an ordinary sentence that happens to contain a colon.
    """
    if not text or any(mark in text for mark in ".?!"):
        return False
    words = text.split()
    if not 1 <= len(words) <= MAX_NAME_WORDS:
        return False
    return all(
        word[:1].isupper() or word[:1].isdigit() or word.casefold() in _NAME_CONNECTORS
        for word in words
    )


def org_words(email: str) -> list[str]:
    """`santi@agentleadlab.com` -> `["agent", "lead", "lab"]`, best effort.

    Only the squashed domain is known - `agentleadlab` - so this can't split it
    into words on its own. It doesn't need to: the caller compares against a
    name with its spaces removed.
    """
    domain = (email or "").split("@")[-1].split(".")[0]
    return [domain.casefold()] if domain else []


def strip_org(name: str, host_email: str) -> str:
    """`Santiago Villegas Agent Lead Lab` -> `Santiago Villegas`.

    Everyone on the team has the company bolted onto their Zoom display name.
    On a card titled after the people on the call it is noise, and it is the
    same noise every time.
    """
    squashed_domain = "".join(org_words(host_email))
    if not squashed_domain:
        return name
    words = name.split()
    for take in range(1, len(words)):
        tail = "".join(words[len(words) - take :]).casefold()
        if tail == squashed_domain:
            return " ".join(words[: len(words) - take])
    return name


def name_from_email(email: str) -> str:
    """`santi@agentleadlab.com` -> `santi`. Only used to spot the host."""
    return (email or "").split("@")[0].replace(".", " ").replace("_", " ").strip().casefold()


def host_and_guests(vtt: str, host_email: str) -> tuple[str, tuple[str, ...]]:
    """(the closer, everyone else) from a raw VTT transcript.

    The host is whoever's display name lines up with the account the recording
    is on. Everyone else was on the other side of the call.
    """
    people = [strip_org(person, host_email) for person in speakers(vtt)]
    people = [person for person in people if person]
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
        uid=meeting_id(meeting),
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


def meeting_id(meeting: dict) -> str:
    """A stable identity for one recorded meeting.

    Zoom's uuid is the real one. The fallback exists because an identity that
    is sometimes absent is worse than a slightly weaker one that always works.
    """
    uuid = str((meeting or {}).get("uuid") or "").strip()
    if uuid:
        return uuid
    return f"{(meeting or {}).get('id', '')}@{(meeting or {}).get('start_time', '')}"


# Where Zoom puts the passcode a viewer types to play a recording. It is the
# line people paste under the link, and unlike the link it appears in the API.
PASSCODE_FIELDS = ("recording_play_passcode", "password", "recording_password")


def match_passcode(meetings: list[dict], passcode: str) -> ZoomRecording | None:
    """The meeting whose play passcode is the one pasted under the link.

    Compared verbatim - `U^M^s7Bw` is eight random characters, so an exact hit
    is as good as an identifier. Zoom's share link is not resolvable through
    the API at all, which makes this the only exact match available.
    """
    wanted = (passcode or "").strip()
    if len(wanted) < 4:
        return None
    for meeting in meetings or []:
        for field in PASSCODE_FIELDS:
            if str((meeting or {}).get(field) or "").strip() == wanted:
                return as_recording(meeting)
    return None


def readable(meetings: list[dict]) -> list[dict]:
    return [m for m in meetings or [] if pick_transcript((m or {}).get("recording_files") or [])]


def newest_unfiled(
    meetings: list[dict], filed: set[str] | frozenset[str], *, within_days: int = 14
) -> ZoomRecording | None:
    """The most recent readable recording that hasn't been filed yet.

    The last resort, and a sound one: a call is posted within a day or two of
    happening, and every earlier call is already in the gallery. Restricted to
    recent recordings so a quiet week can't reach back and dredge up something
    from last month.
    """
    cutoff = (date.today() - timedelta(days=within_days)).isoformat()
    fresh = [
        meeting for meeting in readable(meetings)
        if meeting_id(meeting) not in (filed or set())
        and str(meeting.get("start_time") or "")[:10] >= cutoff
    ]
    if not fresh:
        return None
    fresh.sort(key=lambda meeting: str(meeting.get("start_time") or ""), reverse=True)
    return as_recording(fresh[0])


# What Zoom appends to a recording's name on its share page. The topic in the
# API has none of this, so it comes off before the two are compared.
_PAGE_SUFFIXES = (
    " - shared screen with speaker view",
    " - shared screen with gallery view",
    " - shared screen",
    " - speaker view",
    " - gallery view",
    " - audio only",
    " | zoom",
    " - zoom",
)

_PAGE_TITLE = re.compile(
    r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"'](?P<value>[^\"']+)",
    re.IGNORECASE,
)
_HTML_TITLE = re.compile(r"<title[^>]*>(?P<value>.*?)</title>", re.IGNORECASE | re.DOTALL)


def topic_from_page(html: str) -> str:
    """The recording's name, read off its own share page.

    Zoom's API cannot resolve a share link, but the page behind that link says
    plainly which recording it is - it is what the browser tab shows. The
    passcode gates playback, not the page, so this is readable without one.

    This is the only thing that ties a pasted link to a specific call. Without
    it the choice falls to recency, and recency filed a call with Arlene when
    the link said Derrick.
    """
    for pattern in (_PAGE_TITLE, _HTML_TITLE):
        match = pattern.search(html or "")
        if not match:
            continue
        found = " ".join(match.group("value").split())
        lowered = found.casefold()
        for suffix in _PAGE_SUFFIXES:
            if lowered.endswith(suffix):
                found = found[: len(found) - len(suffix)]
                break
        found = found.strip()
        if found and found.casefold() not in ("zoom", "video conferencing"):
            return found
    return ""


def match_topic(meetings: list[dict], topic: str) -> ZoomRecording | None:
    """The meeting named on the share page, newest first if a name repeats.

    A recurring call carries the same topic every week, so ties are broken by
    recency - among calls that genuinely share a name, which is a far narrower
    guess than recency across the whole account.
    """
    wanted = " ".join((topic or "").split()).casefold()
    if not wanted:
        return None
    hits = [m for m in meetings or [] if str(m.get("topic") or "").strip().casefold() == wanted]
    if not hits:
        return None
    hits.sort(key=lambda meeting: str(meeting.get("start_time") or ""), reverse=True)
    return as_recording(hits[0])


def choose(
    meetings: list[dict],
    *,
    link: str = "",
    passcode: str = "",
    page_topic: str = "",
    filed: set[str] | frozenset[str] | None = None,
) -> tuple[ZoomRecording | None, str]:
    """Which recording was posted, and how that was decided.

    Zoom's API returns a different share token than its web interface does, so
    the link someone pastes cannot be resolved to a meeting - not by better
    matching, not at all. Other developers have hit the same wall. So the link
    is tried, and then the things that *are* comparable take over.

    The reason comes back with the answer so RYTE can say which one it used.
    Deciding by recency is a judgement, and a judgement stated out loud is one
    somebody can correct.
    """
    found = match_share_url(meetings, link)
    if found is not None:
        return found, "the link"

    found = match_topic(meetings, page_topic)
    if found is not None:
        return found, f"the recording being called “{found.topic}”"

    found = match_passcode(meetings, passcode)
    if found is not None:
        return found, "its passcode"

    found = newest_unfiled(meetings, filed or set())
    if found is not None:
        return found, "it being the most recent call not filed yet"

    return None, ""


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

    def share_page_topic(self, share_url: str) -> str:
        """Ask the share page what recording it is. "" if it won't say.

        Deliberately unauthenticated and deliberately forgiving: this is a web
        page, not an API, and it is allowed to change or refuse. Everything
        downstream works without it - it just works better with it.
        """
        if not (share_url or "").strip():
            return ""
        try:
            response = self._client.get(
                share_url,
                follow_redirects=True,
                headers={
                    # Zoom serves a barer page to something that doesn't look
                    # like a browser, and the name is what we came for.
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                    ),
                    "Accept": "text/html",
                },
                timeout=20.0,
            )
        except httpx.HTTPError:
            return ""
        if response.status_code >= 400:
            return ""
        return topic_from_page(response.text)

    def transcript(self, recording: ZoomRecording) -> str:
        """The transcript as plain prose, for summarising."""
        from .youtube import parse_captions

        return parse_captions(self.transcript_vtt(recording))

    def transcript_vtt(self, recording: ZoomRecording) -> str:
        """The transcript exactly as Zoom wrote it, speaker labels intact.

        The labels are the only record of who was on the call, and flattening
        them into prose first loses the line boundaries that make a name a
        name.
        """
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
        return response.text
