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


def fetch_transcript(video_id: str, *, languages: tuple[str, ...] = ("en", "en-US")) -> Transcript:
    """Pull the YouTube transcript, preferring manual captions over auto-generated."""
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=list(languages))
        snippets = [s.text for s in fetched]
    except AttributeError:
        # youtube-transcript-api < 1.0 exposed a classmethod instead.
        raw = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
        snippets = [item["text"] for item in raw]
    except Exception as exc:
        raise IngestError(
            f"No transcript available for {video_id}: {exc}. "
            f"Pass one manually with --transcript <file>."
        ) from exc

    text = _clean_transcript(" ".join(snippets))
    if not text.strip():
        raise IngestError(f"Transcript for {video_id} came back empty.")
    return Transcript(video_id=video_id, text=text, source="youtube")


def load_manual_transcript(video_id: str, path: str | Path) -> Transcript:
    """Read a transcript that was copied out of YouTube by hand."""
    file_path = Path(path)
    if not file_path.exists():
        raise IngestError(f"Transcript file not found: {file_path}")
    text = _clean_transcript(file_path.read_text(encoding="utf-8"))
    if not text.strip():
        raise IngestError(f"Transcript file is empty: {file_path}")
    return Transcript(video_id=video_id, text=text, source="manual")


def _clean_transcript(text: str) -> str:
    """Strip caption artifacts and timestamp lines, collapse whitespace."""
    text = re.sub(r"\[(?:Music|Applause|Laughter|__)\]", " ", text, flags=re.IGNORECASE)
    # Timestamp lines like "0:42" or "01:23:45" that appear in copy-pasted transcripts.
    text = re.sub(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
