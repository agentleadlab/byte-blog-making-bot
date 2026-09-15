"""Putting one file into one Drive folder, and nothing else."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wilbyte import drive

FOLDER = "1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo"


class FakeDrive:
    def __init__(self, status=200):
        self.status = status
        self.sent = []

    def post(self, url, data=None, params=None, headers=None, files=None):
        if url.endswith("/token"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": "t", "expires_in": 3600},
            )
        self.sent.append({"params": params, "files": files})
        return SimpleNamespace(
            status_code=self.status,
            text="no",
            json=lambda: {"id": "file-1", "name": "convo.png"},
        )

    def close(self):
        pass


def client(monkeypatch, *, folder=FOLDER, status=200):
    fake = FakeDrive(status)
    monkeypatch.setattr(drive.httpx, "Client", lambda **kw: fake)
    return drive.DriveClient(
        drive.Credentials("id", "secret", "refresh"), folder=folder,
    ), fake


def test_the_file_goes_into_the_one_folder(monkeypatch, tmp_path):
    """The folder is put into the request here, so nothing calling this can
    write anywhere else in Drive."""
    one, fake = client(monkeypatch)
    shot = tmp_path / "convo.png"
    shot.write_bytes(b"\x89PNG pretend")

    got = one.put(shot)

    meta = json.loads(fake.sent[0]["files"]["metadata"][1])
    assert meta["parents"] == [FOLDER]
    assert meta["name"] == "convo.png"
    assert got.link() == "https://drive.google.com/file/d/file-1/view"


def test_it_can_be_named_something_other_than_the_file(monkeypatch, tmp_path):
    one, fake = client(monkeypatch)
    shot = tmp_path / "tmp123.png"
    shot.write_bytes(b"x")

    one.put(shot, name="Jay Rodriguez — channel.png")

    meta = json.loads(fake.sent[0]["files"]["metadata"][1])
    assert meta["name"] == "Jay Rodriguez — channel.png"


def test_without_a_folder_there_is_nowhere_to_write(monkeypatch):
    """No folder is not "anywhere in Drive" — it is a refusal."""
    with pytest.raises(drive.DriveError) as raised:
        client(monkeypatch, folder="")

    assert "CLIENTS_DRIVE_FOLDER" in str(raised.value)


def test_a_missing_file_is_said_before_anything_is_sent(monkeypatch, tmp_path):
    one, fake = client(monkeypatch)

    with pytest.raises(drive.DriveError):
        one.put(tmp_path / "not-there.png")

    assert fake.sent == []


def test_a_refusal_names_the_two_things_it_can_be(monkeypatch, tmp_path):
    one, _fake = client(monkeypatch, status=403)
    shot = tmp_path / "convo.png"
    shot.write_bytes(b"x")

    with pytest.raises(drive.DriveError) as raised:
        one.put(shot)

    assert "drive.file" in str(raised.value)
    assert "shared with" in str(raised.value)


def test_a_folder_that_is_not_there_says_which_setting(monkeypatch, tmp_path):
    one, _fake = client(monkeypatch, status=404)
    shot = tmp_path / "convo.png"
    shot.write_bytes(b"x")

    with pytest.raises(drive.DriveError) as raised:
        one.put(shot)

    assert "CLIENTS_DRIVE_FOLDER" in str(raised.value)


def test_it_signs_in_with_the_same_token_gmail_uses(monkeypatch):
    """Both were minted together against the same client: one consent with two
    scopes is one thing to keep working rather than two."""
    used = {}

    class Tokens(FakeDrive):
        def post(self, url, data=None, **kwargs):
            if url.endswith("/token"):
                used.update(data or {})
            return super().post(url, data=data, **kwargs)

    fake = Tokens()
    monkeypatch.setattr(drive.httpx, "Client", lambda **kw: fake)
    one = drive.open_drive(SimpleNamespace(
        google_client_id="sheets-id", google_client_secret="sheets-secret",
        google_refresh_token="sheets-token",
        gmail_client_id="ryte-leads", gmail_client_secret="leads-secret",
        gmail_refresh_token="the-one-with-drive",
        clients_drive_folder=f"https://drive.google.com/drive/u/4/folders/{FOLDER}",
    ))
    one._access_token()

    assert used["client_id"] == "ryte-leads"
    assert used["refresh_token"] == "the-one-with-drive"


def test_the_folder_can_be_a_link_or_an_id():
    from wilbyte.gsheets import folder_id_in

    assert folder_id_in(
        f"https://drive.google.com/drive/u/4/folders/{FOLDER}"
    ) == FOLDER
