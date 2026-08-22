"""Read what was actually said in a Loom video.

An SOP filed from a Loom link got a summary saying, at length, that it was a
placeholder and somebody should watch the video. True, and useless: the whole
point of the library is that three weeks later a search for "VA team
operations" finds the thing, and a card whose summary is an apology matches
nothing anybody would type.

Loom auto-captions every video and serves the captions from its own public
GraphQL API - no token, no cookie, no account - for any video that is public
or anyone-with-the-link. Private ones stay private, and say so.

Two calls: the video's metadata for its title, then the transcript, which
comes back as a URL to a caption file rather than as text. The captions are
VTT, the same shape Zoom hands over, so the cue parsing is borrowed from
there rather than written twice.
"""

from __future__ import annotations

import re

import httpx

GRAPHQL = "https://www.loom.com/graphql"

# Loom's own web client sends these and rejects requests that don't. The
# version string travels with them; it is a client build id, not an API
# version, so it going stale is a thing to expect rather than a bug.
CLIENT = "45a5bd4"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.loom.com",
    "x-loom-request-source": f"loom_web_{CLIENT}",
    "apollographql-client-name": "web",
    "apollographql-client-version": CLIENT,
}

SHARE_RE = re.compile(
    r"loom\.com/(?:share|embed|v)/([0-9a-f]{32})", re.IGNORECASE
)

TRANSCRIPT_QUERY = """
query FetchVideoTranscript($videoId: ID!, $password: String) {
  fetchVideoTranscript(videoId: $videoId, password: $password) {
    ... on VideoTranscriptDetails { captions_source_url source_url }
    ... on GenericError { message }
  }
}
"""

TITLE_QUERY = """
query GetVideoSSR($videoId: ID!, $password: String) {
  getVideo(id: $videoId, password: $password) {
    ... on RegularUserVideo { id name description }
    ... on GenericError { message }
  }
}
"""


class LoomError(RuntimeError):
    """Raised when a Loom video can't be read."""


def video_id(url: str) -> str:
    """The 32-character id out of a share link, or "" if it isn't one."""
    found = SHARE_RE.search(url or "")
    return found.group(1).lower() if found else ""


def _ask(operation: str, query: str, variables: dict, *, timeout: float) -> dict:
    try:
        response = httpx.post(
            GRAPHQL,
            json={"operationName": operation, "query": query, "variables": variables},
            headers={**HEADERS, "graphql-operation-name": operation},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise LoomError(f"couldn't reach Loom: {exc}") from exc
    if response.status_code >= 400:
        raise LoomError(f"Loom said HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise LoomError("Loom answered with something that isn't JSON") from exc
    return (body or {}).get("data") or {}


def title(url: str, *, timeout: float = 15.0) -> str:
    """The video's own name, which is what the card gets called."""
    found = video_id(url)
    if not found:
        return ""
    try:
        data = _ask("GetVideoSSR", TITLE_QUERY, {"videoId": found}, timeout=timeout)
    except LoomError:
        return ""
    video = data.get("getVideo") or {}
    return " ".join(str(video.get("name") or "").split())[:120]


def transcript(url: str, *, timeout: float = 20.0) -> str:
    """What was said in the video, as plain text.

    Empty rather than raised when Loom simply has no captions for it: a video
    too new to have been processed, or one with no speech in it, is not an
    error - it is a video with nothing to read.
    """
    found = video_id(url)
    if not found:
        raise LoomError("that isn't a Loom share link")

    data = _ask("FetchVideoTranscript", TRANSCRIPT_QUERY, {"videoId": found}, timeout=timeout)
    details = data.get("fetchVideoTranscript") or {}
    if details.get("message"):
        # Loom returns its refusals in the body with a 200, so a private video
        # arrives looking like a success.
        raise LoomError(str(details["message"]))

    captions = details.get("captions_source_url") or details.get("source_url") or ""
    if not captions:
        return ""

    try:
        response = httpx.get(captions, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise LoomError(f"couldn't fetch the captions: {exc}") from exc
    if response.status_code >= 400:
        raise LoomError(f"the caption file said HTTP {response.status_code}")

    return spoken(response.text)


def spoken(captions: str) -> str:
    """The words out of a caption file, without the timings or the numbering.

    Both VTT and SRT, because Loom serves one and the other turns up often
    enough that telling them apart is not worth a second function. A line is
    dropped if it is a timestamp, a cue number, or the WEBVTT header; what is
    left is what somebody said.
    """
    timings = re.compile(r"-->")
    numbering = re.compile(r"^\d+$")
    words = []
    for line in (captions or "").splitlines():
        line = line.strip()
        if not line or line.upper().startswith("WEBVTT"):
            continue
        if timings.search(line) or numbering.match(line):
            continue
        if line.startswith(("NOTE ", "STYLE", "REGION")):
            continue
        # Caption cues repeat the last line as often as not.
        cleaned = re.sub(r"<[^>]+>", "", line).strip()
        if cleaned and (not words or words[-1] != cleaned):
            words.append(cleaned)
    return " ".join(words)
