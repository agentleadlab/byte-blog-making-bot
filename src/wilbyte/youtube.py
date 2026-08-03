"""Playlist listing and transcript fetching.

This replaces the manual step where Wil opens each video in the playlist,
clicks "Show transcript", and copies the text into the Claude project.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Transcript, Video

_PLAYLIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([A-Za-z0-9_-]{11})"
)


class IngestError(RuntimeError):
    """Raised when a playlist or transcript cannot be retrieved."""


def extract_playlist_id(url_or_id: str) -> str:
    """Accept a full playlist URL, a watch URL with `list=`, or a bare id."""
    match = _PLAYLIST_ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if url_or_id.startswith(("PL", "UU", "OL", "LL", "FL", "RD")):
        return url_or_id
    raise IngestError(f"Could not find a playlist id in: {url_or_id!r}")


def looks_like_playlist(url_or_id: str) -> bool:
    """True for a playlist URL or a bare playlist id.

    A `list=` parameter wins even on a watch URL, which is the link you get from
    "share" while playing a video inside a playlist. Bare ids are disambiguated
    by length: video ids are always 11 characters, playlist ids are longer.
    """
    text = url_or_id.strip()
    if _PLAYLIST_ID_RE.search(text):
        return True
    return len(text) != 11 and text.startswith(("PL", "UU", "OL", "LL", "FL", "RD"))


def extract_video_id(url_or_id: str) -> str:
    match = _VIDEO_ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    raise IngestError(f"Could not find a video id in: {url_or_id!r}")


def list_playlist_videos(playlist_url_or_id: str, *, limit: int | None = None) -> list[Video]:
    """Return the playlist's videos in playlist order (oldest position first).

    Uses yt-dlp in flat mode - metadata only, no media download.
    """
    from yt_dlp import YoutubeDL  # imported lazily; heavy module

    playlist_id = extract_playlist_id(playlist_url_or_id)
    url = f"https://www.youtube.com/playlist?list={playlist_id}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    if limit:
        opts["playlistend"] = limit

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises a wide variety of errors
        raise IngestError(f"Failed to read playlist {playlist_id}: {exc}") from exc

    entries = [e for e in (info or {}).get("entries") or [] if e]
    if not entries:
        raise IngestError(f"Playlist {playlist_id} returned no videos.")

    videos: list[Video] = []
    for index, entry in enumerate(entries, start=1):
        video_id = entry.get("id")
        if not video_id:
            continue
        duration = entry.get("duration")
        videos.append(
            Video(
                video_id=video_id,
                title=entry.get("title") or "(untitled)",
                url=entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                playlist_index=index,
                duration_seconds=int(duration) if duration else None,
            )
        )
    return videos


def fetch_video(video_url_or_id: str) -> Video:
    """Metadata for a single video, for the one-off `--video` path."""
    from yt_dlp import YoutubeDL

    video_id = extract_video_id(video_url_or_id)
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:
        raise IngestError(f"Failed to read video {video_id}: {exc}") from exc

    info = info or {}
    return Video(
        video_id=video_id,
        title=info.get("title") or "(untitled)",
        url=f"https://www.youtube.com/watch?v={video_id}",
        duration_seconds=int(info["duration"]) if info.get("duration") else None,
    )


def video_from_link(url_or_id: str) -> Video:
    """Build a Video from the id alone, with no network call.

    Used when the transcript is supplied by hand: YouTube may be refusing this
    server entirely, and the title is only a nicety - the copy comes from the
    transcript.
    """
    video_id = extract_video_id(url_or_id)
    return Video(
        video_id=video_id,
        title="",
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _proxy_config():
    """Route transcript requests through a proxy when one is configured.

    YouTube blocks datacenter IP ranges for transcript requests, so a bot on a
    cloud host works intermittently at best - fine for the first few calls, then
    blocked. A residential proxy is the only reliable fix.
    """
    import os

    username = os.getenv("WEBSHARE_PROXY_USERNAME")
    password = os.getenv("WEBSHARE_PROXY_PASSWORD")
    generic = os.getenv("YOUTUBE_PROXY_URL")
    if not (username and password) and not generic:
        return None

    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    if username and password:
        return WebshareProxyConfig(proxy_username=username, proxy_password=password)
    return GenericProxyConfig(http_url=generic, https_url=generic)


def _is_blocked(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("blocking requests", "too many requests", "ipblocked", "429")
    )


def fetch_transcript(
    video_id: str,
    *,
    languages: tuple[str, ...] = ("en", "en-US"),
    attempts: int = 3,
) -> Transcript:
    """Pull the YouTube transcript, preferring manual captions over auto-generated.

    Three ways in, cheapest first: the transcript API, then yt-dlp's caption
    download (a different endpoint, so it often survives a block that stops the
    first), and failing both, an instruction to attach the transcript by hand.
    A throttled response is retried with backoff, but an outright IP block skips
    straight to the fallback - waiting seconds will not lift it.
    """
    import time

    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_config = _proxy_config()
    last_error: Exception | None = None
    snippets: list[str] = []

    for attempt in range(attempts):
        try:
            api = YouTubeTranscriptApi(proxy_config=proxy_config)
            fetched = api.fetch(video_id, languages=list(languages))
            snippets = [s.text for s in fetched]
            break
        except Exception as exc:
            last_error = exc
            # An IP block won't lift on a retry, so go straight to the fallback.
            if _is_blocked(exc) or attempt == attempts - 1:
                break
            time.sleep(2**attempt)

    if not snippets:
        try:
            return fetch_transcript_via_ytdlp(video_id, languages=languages)
        except IngestError as fallback_error:
            blocked = last_error is not None and _is_blocked(last_error)
            detail = (
                "YouTube is blocking this server's IP for transcripts, and the "
                "caption fallback did not work either."
                if blocked
                else f"{_first_line(last_error) if last_error else 'unknown error'}"
            )
            raise IngestError(
                f"Could not get a transcript for {video_id}. {detail} "
                f"Fallback said: {fallback_error}. "
                f"You can attach the transcript as a .txt or .vtt file with the link "
                f"instead, or set WEBSHARE_PROXY_USERNAME/WEBSHARE_PROXY_PASSWORD."
            ) from fallback_error

    text = clean_transcript(" ".join(snippets))
    if not text.strip():
        raise IngestError(f"Transcript for {video_id} came back empty.")
    return Transcript(video_id=video_id, text=text, source="youtube")


def fetch_transcript_via_ytdlp(
    video_id: str, *, languages: tuple[str, ...] = ("en", "en-US", "en-GB")
) -> Transcript:
    """Get captions through yt-dlp instead of the transcript API.

    yt-dlp fetches captions the way the player does, which is a different
    endpoint from the one the transcript API uses - so this often still works
    when that one is IP-blocked. Free, and no proxy needed.
    """
    import tempfile
    from pathlib import Path as _Path

    from yt_dlp import YoutubeDL

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(languages),
            "subtitlesformat": "vtt",
            "outtmpl": str(_Path(tmp) / "%(id)s"),
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as exc:
            raise IngestError(f"yt-dlp could not fetch captions for {video_id}: {_first_line(exc)}") from exc

        files = sorted(_Path(tmp).glob("*.vtt")) or sorted(_Path(tmp).glob("*.srt"))
        if not files:
            raise IngestError(f"No caption track published for {video_id}.")
        raw = files[0].read_text(encoding="utf-8", errors="replace")

    text = clean_transcript(parse_captions(raw))
    if not text.strip():
        raise IngestError(f"Caption file for {video_id} was empty.")
    return Transcript(video_id=video_id, text=text, source="youtube-ytdlp")


_TIMESTAMP_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
_INLINE_TAG = re.compile(r"<[^>]+>")


def parse_captions(raw: str) -> str:
    """Flatten a VTT or SRT caption file into plain prose.

    Auto-generated captions scroll, so the same words appear on several
    consecutive cues; emitting them verbatim triples the transcript. Lines are
    de-duplicated against what was last emitted.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if _TIMESTAMP_LINE.match(line) or "-->" in line:
            continue
        if line.isdigit():  # SRT cue numbers
            continue
        line = _INLINE_TAG.sub("", line).strip()
        if not line:
            continue
        if lines and (line == lines[-1] or line in lines[-1]):
            continue
        # A scrolling cue often repeats the previous line plus new words.
        if lines and line.startswith(lines[-1]):
            lines[-1] = line
            continue
        lines.append(line)
    return " ".join(lines)


def _first_line(exc: Exception) -> str:
    """Transcript errors carry a long help blurb; the first line is the reason."""
    for line in str(exc).splitlines():
        if line.strip():
            return line.strip()[:200]
    return str(exc)[:200]


def load_manual_transcript(video_id: str, path: str | Path) -> Transcript:
    """Read a transcript that was copied out of YouTube by hand."""
    file_path = Path(path)
    if not file_path.exists():
        raise IngestError(f"Transcript file not found: {file_path}")
    text = clean_transcript(file_path.read_text(encoding="utf-8"))
    if not text.strip():
        raise IngestError(f"Transcript file is empty: {file_path}")
    return Transcript(video_id=video_id, text=text, source="manual")


def clean_transcript(text: str) -> str:
    """Strip caption artifacts and timestamp lines, collapse whitespace."""
    text = re.sub(r"\[(?:Music|Applause|Laughter|__)\]", " ", text, flags=re.IGNORECASE)
    # Timestamp lines like "0:42" or "01:23:45" that appear in copy-pasted transcripts.
    text = re.sub(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Kept for the tests and callers that used the private name.
_clean_transcript = clean_transcript
