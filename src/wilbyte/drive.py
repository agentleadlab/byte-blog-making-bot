"""Putting one file into one Drive folder, and nothing else.

Before an agent's channel is deleted, a picture of the conversation goes into
the folder that keeps them - "Punta ulit sa channel and screenshot a portion sa
convo and dito ko inupload". The channel is about to stop existing, so this is
the only copy there will be of what was said in it.

Narrow like the inbox reader is narrow: the folder is set in .env and put into
the request here rather than passed in, so nothing calling this can write
anywhere else in Drive. And `drive.file` is the scope, which is the one that
grants access to files this app creates and to nothing that was already there.

Uploads only. There is no read, no list and no delete in here, so a bug in the
clear-out cannot take the folder with it.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import httpx

from .gsheets import (
    Credentials, SheetsError, credentials, explain_token, folder_id_in,
    why_refused,
)

UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class DriveError(RuntimeError):
    """Anything that stopped an upload, said in English."""


@dataclass
class Uploaded:
    """What Drive gave back: enough to link to it from somewhere else."""

    file_id: str
    name: str

    def link(self) -> str:
        return f"https://drive.google.com/file/d/{self.file_id}/view"


class DriveClient:
    """One signed-in session that can put a file in one folder."""

    def __init__(self, creds: Credentials, *, folder: str, timeout: float = 60.0):
        if not (folder or "").strip():
            raise DriveError(
                "No Drive folder set. CLIENTS_DRIVE_FOLDER in .env is the "
                "folder the conversation screenshots go into, and it is the "
                "only place this can write - without one there is nowhere to "
                "put anything."
            )
        self._creds = creds
        self._folder = folder.strip()
        self._client = httpx.Client(timeout=timeout)
        self._token = ""
        self._token_until = 0.0

    def __enter__(self) -> "DriveClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _access_token(self) -> str:
        import time

        if self._token and time.time() < self._token_until:
            return self._token
        try:
            reply = self._client.post(
                TOKEN_URL,
                data={
                    "client_id": self._creds.client_id,
                    "client_secret": self._creds.client_secret,
                    "refresh_token": self._creds.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"Couldn't reach Google to sign in: {exc}") from exc
        if reply.status_code >= 400:
            raise DriveError(explain_token(reply.status_code, reply.text))
        got = reply.json()
        self._token = str(got.get("access_token") or "")
        self._token_until = time.time() + float(got.get("expires_in") or 3600) - 60
        if not self._token:
            raise DriveError("Google signed us in but sent no access token back.")
        return self._token

    def put(self, path: Path, *, name: str = "") -> Uploaded:
        """Upload one file into the configured folder.

        Multipart in one request: the metadata that says which folder, then
        the bytes. A resumable upload would be the right thing for something
        large, and a screenshot is not.
        """
        where = Path(path)
        if not where.is_file():
            raise DriveError(f"There is no file at {where}")

        called = name or where.name
        kind = mimetypes.guess_type(called)[0] or "application/octet-stream"
        meta = json.dumps({"name": called, "parents": [self._folder]})
        try:
            reply = self._client.post(
                UPLOAD,
                params={"uploadType": "multipart", "fields": "id,name"},
                headers={"Authorization": f"Bearer {self._access_token()}"},
                files={
                    "metadata": ("metadata.json", meta, "application/json"),
                    "file": (called, where.read_bytes(), kind),
                },
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"Couldn't reach Drive: {exc}") from exc

        if reply.status_code == 403:
            raise DriveError(
                "Drive refused that: "
                + (why_refused(reply.text) or "no reason given")
                + "\n-# If that mentions a scope, the one it wants is "
                "https://www.googleapis.com/auth/drive.file. If it mentions "
                "permission, CLIENTS_DRIVE_FOLDER is not shared with the "
                "account the token was minted for."
            )
        if reply.status_code == 404:
            raise DriveError(
                f"Drive has no folder {self._folder}. Check CLIENTS_DRIVE_FOLDER "
                "in .env is the folder's link or its id."
            )
        if reply.status_code >= 400:
            raise DriveError(f"Drive said {reply.status_code}: {reply.text[:200]}")

        got = reply.json()
        return Uploaded(file_id=str(got.get("id") or ""), name=str(got.get("name") or called))


def open_drive(secrets) -> DriveClient:
    """A signed-in client for the one folder, or a sentence about what's missing.

    The same token Gmail uses: both were minted together against the same
    client, because one consent with two scopes is one thing to keep working
    rather than two.
    """
    try:
        creds = credentials(secrets)
    except SheetsError as exc:
        raise DriveError(str(exc).replace("Google Sheets", "Drive")) from exc

    instead = (getattr(secrets, "gmail_refresh_token", "") or "").strip()
    if instead:
        creds = Credentials(
            (getattr(secrets, "gmail_client_id", "") or "").strip() or creds.client_id,
            (getattr(secrets, "gmail_client_secret", "") or "").strip()
            or creds.client_secret,
            instead,
        )
    return DriveClient(
        creds,
        folder=folder_id_in(getattr(secrets, "clients_drive_folder", "") or ""),
    )
