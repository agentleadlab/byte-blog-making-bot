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
    call. The token after `/rec/share/` is the identity.
    """
    text = (url or "").strip()
    marker = "/rec/share/"
    found = text.casefold().find(marker)
    if found >= 0:
        text = text[found + len(marker) :]
    return text.split("?")[0].split("#")[0].strip("/").casefold()


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


def as_recording(meeting: dict) -> ZoomRecording:
    return ZoomRecording(
        topic=str(meeting.get("topic") or "").strip(),
        share_url=str(meeting.get("share_url") or ""),
        host_email=str(meeting.get("host_email") or ""),
        started_at=str(meeting.get("start_time") or ""),
        transcript_url=pick_transcript(meeting.get("recording_files") or []),
    )


def match_share_url(meetings: list[dict], share_url: str) -> ZoomRecording | None:
    """Find the meeting behind a link someone pasted into Discord."""
    wanted = share_key(share_url)
    if not wanted:
        return None
    for meeting in meetings or []:
        if share_key(str(meeting.get("share_url") or "")) == wanted:
            return as_recording(meeting)
    return None


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
