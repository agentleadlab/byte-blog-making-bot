"""YouTube Data API v3 - the sanctioned way in.

YouTube blocks anonymous scraping from datacenter IPs, which is what breaks
yt-dlp and the transcript library on a cloud host. Asking through the official
API as the channel owner is a different thing entirely: authenticated, within
quota, and not subject to bot detection.

Two levels of access, and they unlock different things:

  YOUTUBE_API_KEY        public data - playlist listings and video metadata
  OAuth (3 variables)    captions, which are owner-only

The key alone fixes "Sign in to confirm you're not a bot" on metadata. Captions
need OAuth because YouTube only lets a video's owner download its caption track.
"""

from __future__ import annotations

import os
import time

import httpx

API_ROOT = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

# Access tokens last an hour; refresh a little early rather than racing expiry.
_TOKEN_TTL_MARGIN = 120
_token_cache: dict[str, float | str] = {}


class YouTubeAPIError(RuntimeError):
    """Raised when the Data API refuses a request."""


def api_key() -> str | None:
    return (os.getenv("YOUTUBE_API_KEY") or "").strip() or None


def oauth_credentials() -> tuple[str, str, str] | None:
    """(client_id, client_secret, refresh_token), or None if not configured."""
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
    refresh = (os.getenv("GOOGLE_REFRESH_TOKEN") or "").strip()
    if client_id and secret and refresh:
        return client_id, secret, refresh
    return None


OAUTH_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")


def missing_oauth_vars() -> list[str]:
    """Which of the three OAuth variables are still blank.

    Two of three is the usual state halfway through the setup, and it behaves
    exactly like none of them - so `check` should be able to name the one that
    is missing rather than say "not configured" again.
    """
    return [name for name in OAUTH_VARS if not (os.getenv(name) or "").strip()]


def configured() -> bool:
    return bool(api_key() or oauth_credentials())


def access_token(*, force: bool = False) -> str:
    """Trade the refresh token for an access token, cached until it expires."""
    creds = oauth_credentials()
    if not creds:
        raise YouTubeAPIError(
            "OAuth is not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET "
            "and GOOGLE_REFRESH_TOKEN to read captions."
        )

    now = time.time()
    if not force and _token_cache.get("token") and float(_token_cache.get("expires", 0)) > now:
        return str(_token_cache["token"])

    client_id, secret, refresh = creds
    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise YouTubeAPIError(f"Could not reach Google to refresh the token: {exc}") from exc

    if response.status_code >= 400:
        raise YouTubeAPIError(_explain_refresh(response))

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise YouTubeAPIError(f"No access token in Google's reply: {payload}")

    _token_cache["token"] = token
    _token_cache["expires"] = now + float(payload.get("expires_in", 3600)) - _TOKEN_TTL_MARGIN
    return token


def _get(path: str, params: dict, *, use_oauth: bool = False) -> dict:
    """One Data API call, authenticating with OAuth or the API key."""
    headers = {}
    params = dict(params)
    if use_oauth:
        headers["Authorization"] = f"Bearer {access_token()}"
    else:
        key = api_key()
        if key:
            params["key"] = key
        elif oauth_credentials():
            headers["Authorization"] = f"Bearer {access_token()}"
        else:
            raise YouTubeAPIError("No YOUTUBE_API_KEY and no OAuth credentials configured.")

    try:
        response = httpx.get(f"{API_ROOT}/{path}", params=params, headers=headers, timeout=60)
    except httpx.HTTPError as exc:
        raise YouTubeAPIError(f"YouTube API request failed: {exc}") from exc

    if response.status_code >= 400:
        raise YouTubeAPIError(_explain(path, response))
    return response.json()


def _explain_refresh(response: httpx.Response) -> str:
    """Say which of the three OAuth values is actually wrong.

    Google's two rejections here mean opposite things and have opposite fixes,
    and neither has anything to do with who owns the channel - so they must not
    be reported as an ownership problem.
    """
    body = response.text[:300]

    if "unauthorized_client" in body:
        return (
            "GOOGLE_REFRESH_TOKEN does not belong to this GOOGLE_CLIENT_ID. The "
            "token was minted against a different OAuth client - usually because "
            "'Use your own OAuth credentials' was unticked in the OAuth "
            "Playground's gear panel, so Google issued a token for the "
            "playground's own client. Tick it, paste the same client id and "
            "secret that are set here, and mint the token again."
        )

    if "invalid_grant" in body:
        return (
            "GOOGLE_REFRESH_TOKEN is no longer valid. Refresh tokens die when the "
            "OAuth client is edited, when access is revoked, or if an access "
            "token (ya29...) was pasted in place of the refresh token (1//...). "
            "Mint a fresh one in the OAuth Playground."
        )

    return (
        f"Google rejected the refresh token (HTTP {response.status_code}): {body}. "
        f"Mint a new one in the OAuth Playground."
    )


def _explain(path: str, response: httpx.Response) -> str:
    """Turn Google's error envelope into something actionable."""
    body = response.text[:400]
    code = response.status_code
    if code == 403 and "quotaExceeded" in body:
        return (
            "YouTube API daily quota is used up. It resets at midnight Pacific. "
            "Raise it in the Google Cloud console if this keeps happening."
        )
    if code == 403 and ("forbidden" in body.lower() or "permission" in body.lower()):
        return (
            f"YouTube refused {path} (403). Captions can only be downloaded by the "
            f"account that owns the video - make sure the OAuth consent was given "
            f"as the Agent Lead Lab channel. Detail: {body}"
        )
    if code == 401:
        return f"YouTube rejected the credentials for {path} (401). Detail: {body}"
    if code == 404:
        return f"YouTube has no such resource for {path} (404). Detail: {body}"
    return f"YouTube API {path} -> HTTP {code}: {body}"


# --------------------------------------------------------------------- reading


def get_video(video_id: str) -> dict:
    """Title and duration for one video."""
    data = _get("videos", {"part": "snippet,contentDetails", "id": video_id})
    items = data.get("items") or []
    if not items:
        raise YouTubeAPIError(f"No video found with id {video_id}.")
    return items[0]


def list_playlist_items(playlist_id: str, *, limit: int | None = None) -> list[dict]:
    """Every video in a playlist, following pagination."""
    items: list[dict] = []
    page_token = None

    while True:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = _get("playlistItems", params)
        items.extend(data.get("items") or [])
        if limit and len(items) >= limit:
            return items[:limit]

        page_token = data.get("nextPageToken")
        if not page_token:
            return items


def list_captions(video_id: str) -> list[dict]:
    """Caption tracks on a video. Requires OAuth as the video's owner."""
    data = _get("captions", {"part": "snippet", "videoId": video_id}, use_oauth=True)
    return data.get("items") or []


def rank_caption_tracks(tracks: list[dict], languages: tuple[str, ...]) -> list[dict]:
    """Every track, best first: human-written and in a wanted language wins.

    Manual captions are punctuated and correctly cased, which produces markedly
    better copy than ASR output. The whole list is returned rather than just the
    winner because `captions.download` can refuse an individual track that
    `captions.list` happily showed - so the caller gets to try the next one.
    """
    def language(track: dict) -> str:
        return (track.get("snippet") or {}).get("language", "")

    def is_asr(track: dict) -> bool:
        return (track.get("snippet") or {}).get("trackKind") == "ASR"

    wanted = [lang.lower() for lang in languages]

    def rank(track: dict) -> tuple[int, int]:
        base = language(track).lower().split("-")[0]
        position = next(
            (i for i, lang in enumerate(wanted) if base == lang.split("-")[0]), len(wanted)
        )
        return (int(is_asr(track)), position)

    return sorted(tracks, key=rank)


def pick_caption_track(tracks: list[dict], languages: tuple[str, ...]) -> dict | None:
    """The single best caption track, or None if there are none at all."""
    ranked = rank_caption_tracks(tracks, languages)
    return ranked[0] if ranked else None


def download_caption(caption_id: str, *, fmt: str = "srt") -> str:
    """Fetch one caption track's text. Requires OAuth as the owner."""
    try:
        response = httpx.get(
            f"{API_ROOT}/captions/{caption_id}",
            params={"tfmt": fmt},
            headers={"Authorization": f"Bearer {access_token()}"},
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise YouTubeAPIError(f"Caption download failed: {exc}") from exc

    if response.status_code >= 400:
        raise YouTubeAPIError(_explain(f"captions/{caption_id}", response))
    return response.text
