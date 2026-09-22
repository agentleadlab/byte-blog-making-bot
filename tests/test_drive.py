"""Putting one file into one Drive folder, and nothing else."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wilbyte import drive

FOLDER = "1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo"


class FakeDrive:
    def __init__(self, status=200, *, found=None, looking=200, making=200):
        self.status = status
        self.found = found if found is not None else []
        self.looking = looking
        self.making = making
        self.sent = []
        self.asked = []
        self.made = []

    def get(self, url, params=None, headers=None):
        self.asked.append(params)
        return SimpleNamespace(
            status_code=self.looking, text="no",
            json=lambda: {"files": list(self.found)},
        )

    def post(self, url, data=None, params=None, headers=None, files=None,
             json=None):
        if url.endswith("/token"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": "t", "expires_in": 3600},
            )
        if json is not None:
            self.made.append(json)
            return SimpleNamespace(
                status_code=self.making, text="no",
                json=lambda: {"id": "folder-made"},
            )
        self.sent.append({"params": params, "files": files})
        return SimpleNamespace(
            status_code=self.status,
            text="no",
            json=lambda: {"id": "file-1", "name": "convo.png"},
        )

    def close(self):
        pass


def client(monkeypatch, *, folder=FOLDER, status=200, found=None, looking=200,
           making=200):
    fake = FakeDrive(status, found=found, looking=looking, making=making)
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


# --------------- a folder each, inside the one from .env


def test_a_client_who_has_a_folder_already_keeps_it(monkeypatch):
    """A client cleared out twice is one folder with both days in it, not two
    folders with the same name that nobody can tell apart."""
    one, fake = client(monkeypatch, found=[{"id": "theirs", "name": "Artur"}])

    assert one.folder_named("Artur") == "theirs"
    assert fake.made == [], "a folder that was already there was made again"


def test_a_client_with_no_folder_gets_one_inside_the_configured_folder(monkeypatch):
    one, fake = client(monkeypatch, found=[])

    assert one.folder_named("Artur") == "folder-made"
    assert fake.made[0]["parents"] == [FOLDER], "made outside the one folder"
    assert fake.made[0]["mimeType"] == drive.A_FOLDER
    assert fake.made[0]["name"] == "Artur"


def test_the_lookup_is_pinned_to_the_one_folder(monkeypatch):
    """The worst a bad name can do is fail to match."""
    one, fake = client(monkeypatch, found=[])
    one.folder_named("Artur")

    asked = fake.asked[0]["q"]
    assert f"'{FOLDER}' in parents" in asked
    assert "trashed = false" in asked
    assert drive.A_FOLDER in asked


def test_a_name_with_an_apostrophe_cannot_rewrite_the_query(monkeypatch):
    """O'Brien is a client, and ' or '' is a way of asking for everything."""
    one, fake = client(monkeypatch, found=[])
    one.folder_named("O'Brien")

    asked = fake.asked[0]["q"]
    assert "name = 'O\\'Brien'" in asked
    assert fake.made[0]["name"] == "O'Brien", "the name Drive stores is the real one"


def test_nobody_named_means_the_folder_from_env(monkeypatch):
    one, fake = client(monkeypatch)

    assert one.folder_named("") == FOLDER
    assert fake.asked == [] and fake.made == []


def test_drive_falling_over_on_the_lookup_does_not_make_a_second_folder(monkeypatch):
    """A 500 is not "there isn't one". Making one on that answer is how a
    client ends up with two folders and half their conversation in each."""
    one, _fake = client(monkeypatch, looking=503)

    with pytest.raises(drive.DriveError) as complaint:
        one.folder_named("Artur")

    assert "503" in str(complaint.value)


def test_being_refused_the_folder_says_what_to_change(monkeypatch):
    one, _fake = client(monkeypatch, found=[], making=403)

    with pytest.raises(drive.DriveError) as complaint:
        one.folder_named("Artur")

    assert "drive.file" in str(complaint.value)


def test_a_folder_made_with_no_id_is_not_treated_as_one(monkeypatch):
    one, fake = client(monkeypatch, found=[])
    fake.made = []

    class Quiet(FakeDrive):
        def post(self, url, data=None, params=None, headers=None, files=None,
                 json=None):
            if json is not None:
                return SimpleNamespace(status_code=200, text="",
                                       json=lambda: {})
            return super().post(url, data, params, headers, files, json)

    monkeypatch.setattr(drive.httpx, "Client", lambda **kw: Quiet(found=[]))
    two = drive.DriveClient(
        drive.Credentials("id", "secret", "refresh"), folder=FOLDER,
    )

    with pytest.raises(drive.DriveError):
        two.folder_named("Artur")


def test_the_picture_goes_into_their_folder_rather_than_the_top_one(monkeypatch, tmp_path):
    one, fake = client(monkeypatch)
    shot = tmp_path / "convo.png"
    shot.write_bytes(b"\x89PNG pretend")

    one.put(shot, name="1 — Artur — ring-da-bell.png", into="theirs")

    meta = json.loads(fake.sent[0]["files"]["metadata"][1])
    assert meta["parents"] == ["theirs"]


def test_no_folder_given_still_means_the_one_from_env(monkeypatch, tmp_path):
    one, fake = client(monkeypatch)
    shot = tmp_path / "convo.png"
    shot.write_bytes(b"\x89PNG pretend")

    one.put(shot, into="")

    meta = json.loads(fake.sent[0]["files"]["metadata"][1])
    assert meta["parents"] == [FOLDER]
