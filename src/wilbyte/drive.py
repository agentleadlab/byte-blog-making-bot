"""Putting one file into one Drive folder, and nothing else.

Before an agent's channel is deleted, a picture of the conversation goes into
the folder that keeps them - "Punta ulit sa channel and screenshot a portion sa
convo and dito ko inupload". The channel is about to stop existing, so this is
the only copy there will be of what was said in it.

Narrow like the inbox reader is narrow: the folder is set in .env and put into
the request here rather than passed in, so nothing calling this can write
anywhere else in Drive. And `drive.file` is the scope, which is the one that
grants access to files this app creates and to nothing that was already there.

Uploads, and the folders to put them in. Each client gets a folder of their
own inside the one from .env, so opening it shows their conversation rather
than a heap of loose pictures with every other client's mixed in.

No delete, no move, no overwrite. The only read is the one that asks whether
this client's folder already exists, and under `drive.file` even that can only
see folders this app made itself - so a bug in the clear-out cannot take the
folder with it.
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
FILES = "https://www.googleapis.com/drive/v3/files"
A_FOLDER = "application/vnd.google-apps.folder"
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

    def folder_named(self, name: str) -> str:
        """The folder of that name inside the configured one, made if needed.

        Asked for rather than assumed: a client cleared out twice is one
        folder with both days in it, not two folders with the same name that
        nobody can tell apart.

        The `q` is pinned to the configured folder as its parent, so the
        worst a bad name can do is fail to match. Under `drive.file` the
        listing only ever sees folders this app created anyway.
        """
        called = " ".join(str(name or "").split())
        if not called:
            return self._folder
        safe = called.replace("\\", "\\\\").replace("'", "\\'")
        try:
            reply = self._client.get(
                FILES,
                params={
                    "q": (
                        f"name = '{safe}' and '{self._folder}' in parents "
                        f"and mimeType = '{A_FOLDER}' and trashed = false"
                    ),
                    "fields": "files(id,name)",
                    "pageSize": 1,
                },
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"Couldn't reach Drive: {exc}") from exc
        if reply.status_code < 400:
            found = (reply.json().get("files") or [])
            if found and found[0].get("id"):
                return str(found[0]["id"])
        elif reply.status_code >= 500:
            raise DriveError(f"Drive said {reply.status_code} looking for that folder.")
        # A 4xx on the lookup is not fatal on its own - the create below says
        # the same thing in a sentence that names the folder.

        try:
            made = self._client.post(
                FILES,
                params={"fields": "id"},
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json={
                    "name": called, "mimeType": A_FOLDER,
                    "parents": [self._folder],
                },
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"Couldn't reach Drive: {exc}") from exc
        if made.status_code >= 400:
            raise DriveError(self._refused(made.status_code, made.text))
        got = str((made.json() or {}).get("id") or "")
        if not got:
            raise DriveError("Drive made that folder but sent no id back.")
        return got

    def _refused(self, code: int, body: str) -> str:
        """Why Drive said no, in a sentence that says what to change."""
        if code == 403:
            return (
                "Drive refused that: "
                + (why_refused(body) or "no reason given")
                + "\n-# If that mentions a scope, the one it wants is "
                "https://www.googleapis.com/auth/drive.file. If it mentions "
                "permission, CLIENTS_DRIVE_FOLDER is not shared with the "
                "account the token was minted for."
            )
        if code == 404:
            return (
                f"Drive has no folder {self._folder}. Check CLIENTS_DRIVE_FOLDER "
                "in .env is the folder's link or its id."
            )
        return f"Drive said {code}: {body[:200]}"

    def put(self, path: Path, *, name: str = "", into: str = "") -> Uploaded:
        """Upload one file into the configured folder, or one inside it.

        Multipart in one request: the metadata that says which folder, then
        the bytes. A resumable upload would be the right thing for something
        large, and a screenshot is not.
        """
        where = Path(path)
        if not where.is_file():
            raise DriveError(f"There is no file at {where}")

        called = name or where.name
        kind = mimetypes.guess_type(called)[0] or "application/octet-stream"
        meta = json.dumps({
            "name": called, "parents": [(into or "").strip() or self._folder],
        })
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

        if reply.status_code >= 400:
            raise DriveError(self._refused(reply.status_code, reply.text))

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
