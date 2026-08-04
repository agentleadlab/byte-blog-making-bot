"""Playlist listing and transcript fetching.

This replaces the manual step where Wil opens each video in the playlist,
clicks "Show transcript", and copies the text into the Claude project.
"""

from __future__ import annotations

import os
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

    Prefers the official Data API when configured - yt-dlp gets blocked from
    datacenter IPs, and the API does not.
    """
    from yt_dlp import YoutubeDL  # imported lazily; heavy module

    from . import youtube_api

    playlist_id = extract_playlist_id(playlist_url_or_id)

    if youtube_api.configured():
        try:
            items = youtube_api.list_playlist_items(playlist_id, limit=limit)
            videos = []
            for index, item in enumerate(items, start=1):
                snippet = item.get("snippet") or {}
                video_id = (item.get("contentDetails") or {}).get("videoId") or (
                    snippet.get("resourceId") or {}
                ).get("videoId")
                if not video_id:
                    continue
                videos.append(
                    Video(
                        video_id=video_id,
                        title=snippet.get("title") or "(untitled)",
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        playlist_index=index,
                    )
                )
            if videos:
                return videos
        except youtube_api.YouTubeAPIError:
            pass  # fall through to yt-dlp

    url = f"https://www.youtube.com/playlist?list={playlist_id}"

    opts = _ydl_opts(extract_flat="in_playlist")
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

    from . import youtube_api

    video_id = extract_video_id(video_url_or_id)

    if youtube_api.configured():
        try:
            item = youtube_api.get_video(video_id)
            return Video(
                video_id=video_id,
                title=(item.get("snippet") or {}).get("title") or "(untitled)",
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        except youtube_api.YouTubeAPIError:
            pass  # fall through to yt-dlp

    try:
        with YoutubeDL(_ydl_opts()) as ydl:
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


_COOKIE_CACHE: dict[str, str] = {}


def cookie_file() -> str | None:
    """Path to a Netscape cookies.txt for yt-dlp, if one is configured.

    This is the route that actually beats "Sign in to confirm you're not a bot"
    from a datacenter: signed-in requests aren't subject to the check. The
    cookies come from any logged-in YouTube session - it does not have to be the
    channel owner, so a throwaway account is fine and safer.

    YOUTUBE_COOKIES holds the file's contents (that's what fits in a Railway
    variable); YOUTUBE_COOKIES_FILE points at one already on disk.
    """
    import tempfile
    from pathlib import Path as _Path

    existing = (os.getenv("YOUTUBE_COOKIES_FILE") or "").strip()
    if existing and _Path(existing).exists():
        return existing

    raw = os.getenv("YOUTUBE_COOKIES") or ""
    # Railway strips real newlines out of some pasted values; accept the escaped
    # form too rather than writing a one-line file yt-dlp can't parse.
    text = raw.replace("\\n", "\n").strip()
    if not text:
        return None

    cached = _COOKIE_CACHE.get(text)
    if cached and _Path(cached).exists():
        return cached

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix="yt-cookies-", delete=False, encoding="utf-8"
    )
    with handle:
        if not text.startswith("# Netscape"):
            handle.write("# Netscape HTTP Cookie File\n")
        handle.write(_retab_cookies(text) + "\n")
    _COOKIE_CACHE[text] = handle.name
    return handle.name


def _retab_cookies(text: str) -> str:
    """Put the tabs back into a cookies.txt that lost them on the way here.

    The format is tab-separated, and pasting a file through a web form very
    often turns those tabs into runs of spaces. yt-dlp then reads a file with
    no usable cookies in it and fails exactly as though none were given, which
    is a miserable thing to debug.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "\t" in line:
            lines.append(line)
            continue
        fields = stripped.split()
        # domain, include-subdomains, path, secure, expiry, name, value. A value
        # containing spaces would over-split, so only rejoin an exact match.
        if len(fields) == 7:
            lines.append("\t".join(fields))
        else:
            lines.append(line)
    return "\n".join(lines)


# The cookies that actually carry a YouTube login. Without one of these the
# file parses fine and the request still goes out anonymous.
_LOGIN_COOKIES = ("__Secure-3PSID", "__Secure-1PSID", "SAPISID", "SID")


def cookie_summary() -> tuple[int, bool]:
    """(usable cookie lines, whether a login cookie is among them).

    An export taken from a logged-out tab produces a perfectly valid file with
    no session in it - indistinguishable from a good one until a fetch fails.
    """
    path = cookie_file()
    if not path:
        return 0, False
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, False

    names = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split("\t")
        if len(fields) >= 6:
            names.append(fields[5])
    return len(names), any(name in _LOGIN_COOKIES for name in names)


def _ydl_opts(**extra) -> dict:
    """Base yt-dlp options, with cookies attached when they are configured."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, **extra}
    cookies = cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _proxy_config():
    """Route transcript requests through a proxy when one is configured.

    YouTube blocks datacenter IP ranges for transcript requests, so a bot on a
    cloud host works intermittently at best - fine for the first few calls, then
    blocked. A residential proxy is the only reliable fix.
    """
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

    Several ways in, best first: the Data API when OAuth is set, yt-dlp with
    cookies (a signed-in request, so the bot check doesn't apply), then the
    anonymous transcript API and yt-dlp without cookies. Whichever answers
    first wins.

    Every failure is kept and reported together. Routes fail for unrelated
    reasons - an API permission refusal says nothing about why cookies didn't
    work - and showing only the first one turns diagnosis into guesswork.
    """
    import time

    from youtube_transcript_api import YouTubeTranscriptApi

    from . import youtube_api

    failures: list[tuple[str, Exception]] = []

    if youtube_api.oauth_credentials():
        try:
            return fetch_transcript_via_api(video_id, languages=languages)
        except (youtube_api.YouTubeAPIError, IngestError) as exc:
            failures.append(("Data API", exc))

    proxy_config = _proxy_config()
    have_cookies = bool(cookie_file())

    # Cookies make yt-dlp a signed-in request, which is not subject to the bot
    # check - so when they're set it goes ahead of the anonymous transcript API
    # rather than waiting behind three doomed retries of it.
    if have_cookies and not proxy_config:
        try:
            return fetch_transcript_via_ytdlp(video_id, languages=languages)
        except IngestError as exc:
            failures.append(("cookies", exc))

    snippets: list[str] = []
    for attempt in range(attempts):
        try:
            api = YouTubeTranscriptApi(proxy_config=proxy_config)
            fetched = api.fetch(video_id, languages=list(languages))
            snippets = [s.text for s in fetched]
            break
        except Exception as exc:
            if attempt == attempts - 1 or _is_blocked(exc):
                # An IP block won't lift on a retry; go straight to the fallback.
                failures.append(("transcript API", exc))
                break
            time.sleep(2**attempt)

    if snippets:
        text = clean_transcript(" ".join(snippets))
        if text.strip():
            return Transcript(video_id=video_id, text=text, source="youtube")
        failures.append(("transcript API", IngestError("came back empty")))

    if not have_cookies or proxy_config:
        try:
            return fetch_transcript_via_ytdlp(video_id, languages=languages)
        except IngestError as exc:
            failures.append(("yt-dlp", exc))

    raise IngestError(_transcript_failure(video_id, failures))


def _transcript_failure(video_id: str, failures: list[tuple[str, Exception]]) -> str:
    """One message naming every route that was tried and what it said."""
    if not failures:
        return f"Could not get a transcript for {video_id}."

    lines = [f"{route}: {_first_line(exc)}" for route, exc in failures]
    advice = "Attach the transcript as a .txt with the link and everything else runs."

    joined = " ".join(str(exc) for _, exc in failures).lower()
    if "403" in joined or "permission" in joined:
        advice = (
            "Captions made in YouTube Studio can't be downloaded through the Data "
            "API even by the owner - cookies are the route that works. " + advice
        )

    return f"Could not get a transcript for {video_id}. Tried — " + " | ".join(lines) + f". {advice}"


def fetch_transcript_via_api(
    video_id: str, *, languages: tuple[str, ...] = ("en", "en-US", "en-GB")
) -> Transcript:
    """Get captions through the YouTube Data API, as the channel owner."""
    from . import youtube_api

    tracks = youtube_api.list_captions(video_id)
    if not tracks:
        raise IngestError(f"No caption track published for {video_id}.")

    # `captions.download` applies a stricter ownership rule than `captions.list`,
    # so a track that was listed can still be refused. Work down the ranking
    # rather than giving up on the first refusal.
    ranked = youtube_api.rank_caption_tracks(tracks, languages)
    text = ""
    track = None
    last_error: Exception | None = None
    for candidate in ranked:
        try:
            raw = youtube_api.download_caption(candidate["id"])
        except youtube_api.YouTubeAPIError as exc:
            last_error = exc
            continue
        text = clean_transcript(parse_captions(raw))
        if text.strip():
            track = candidate
            break

    if track is None:
        if last_error is not None:
            raise IngestError(str(last_error)) from last_error
        raise IngestError(f"Every caption track for {video_id} was empty.")

    kind = (track.get("snippet") or {}).get("trackKind", "")
    return Transcript(
        video_id=video_id,
        text=text,
        source="youtube-api-asr" if kind == "ASR" else "youtube-api",
    )


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
        opts = _ydl_opts(
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=list(languages),
            subtitlesformat="vtt",
            outtmpl=str(_Path(tmp) / "%(id)s"),
        )
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
