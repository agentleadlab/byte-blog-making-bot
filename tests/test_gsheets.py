"""What Google says when it refuses, and what that means to do about it."""

from __future__ import annotations

import json

import pytest

from wilbyte import gsheets


def google_says(error: str, description: str = "") -> str:
    return json.dumps({"error": error, "error_description": description})


def test_the_wrong_client_is_named_as_the_wrong_client():
    """Google says only "Unauthorized", which reads like a bad token and sends
    somebody back to mint the same one again. The token and the client are
    both real; they are just not each other's."""
    said = gsheets.explain_token(401, google_says("unauthorized_client", "Unauthorized"))

    assert "different OAuth client" in said
    assert "Use your own OAuth credentials" in said


def test_an_expired_grant_says_what_expires_it():
    said = gsheets.explain_token(400, google_says("invalid_grant", "Bad Request"))

    assert "Testing mode" in said
    assert "GOOGLE_REFRESH_TOKEN" in said


def test_a_missing_scope_points_at_the_sharing_and_the_scope():
    said = gsheets.explain(403, json.dumps(
        {"error": {"message": "Request had insufficient authentication scopes."}}
    ))

    assert "403" in said and "Sheets scope" in said


def test_a_missing_tab_is_a_missing_tab():
    assert "404" in gsheets.explain(404, "{}")


def test_rate_limiting_says_to_wait_rather_than_to_fix_something():
    assert "minute" in gsheets.explain(429, "{}")


def test_an_error_body_that_is_not_json_is_still_shown():
    assert "went wrong" in gsheets.explain(500, "something went wrong")


@pytest.mark.parametrize("name", ["client_id", "client_secret", "refresh_token"])
def test_a_missing_credential_is_named(name):
    class Half:
        google_client_id = "id"
        google_client_secret = "secret"
        google_refresh_token = "token"

    setattr(Half, f"google_{name}", None)

    with pytest.raises(gsheets.SheetsError) as raised:
        gsheets.credentials(Half())

    assert f"GOOGLE_{name.upper()}" in str(raised.value)


# ------------------------------------------------- where a write landed


@pytest.mark.parametrize(
    "span,wanted",
    [
        ("August!A7:E10", (7, 10)),
        ("'August 2026'!A2:G2", (2, 2)),
        ("Sheet1!A1:Z1000", (1, 1000)),
        ("", None),
        ("nothing like a range", None),
    ],
)
def test_the_rows_a_write_landed_on(span, wanted):
    """A row has to be formatted after it is written, and the range Sheets
    hands back is the only thing that knows where it went."""
    assert gsheets.rows_in(span) == wanted

# --------------------------------- a link pasted out of the browser bar

CLIENTS_LINK = (
    "https://docs.google.com/spreadsheets/d/"
    "1uqe60vvlAPrON-Owqw8IBETurkJyyZ2U5Ix8xkDyM3o/edit?gid=1765528573#gid=1765528573"
)


def test_the_spreadsheet_and_the_tab_both_come_out_of_the_link():
    """A link carries the gid rather than the tab's name, and every write is
    addressed by name — so nobody has to read the tab strip and copy it."""
    assert gsheets.sheet_id_in(CLIENTS_LINK) == (
        "1uqe60vvlAPrON-Owqw8IBETurkJyyZ2U5Ix8xkDyM3o"
    )
    assert gsheets.gid_in(CLIENTS_LINK) == "1765528573"


def test_a_link_with_no_tab_in_it_says_so_rather_than_guessing():
    plain = "https://docs.google.com/spreadsheets/d/1uqe60vvlAPrON-Owqw8IBET/edit"

    assert gsheets.sheet_id_in(plain) == "1uqe60vvlAPrON-Owqw8IBET"
    assert gsheets.gid_in(plain) == ""


@pytest.mark.parametrize(
    "said, expected",
    [
        ("https://drive.google.com/drive/u/4/folders/1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo",
         "1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo"),
        ("1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo", "1dV9WSXRbiVoQ9LhznL2u1gM9J3_ZFeqo"),
        ("", ""),
        ("not a folder", ""),
    ],
)
def test_a_drive_folder_by_link_or_by_id(said, expected):
    assert gsheets.folder_id_in(said) == expected


def test_the_tab_is_resolved_from_the_gid():
    class Book:
        def tabs(self, sheet_id):
            return [
                {"sheetId": 0, "title": "Sheet1"},
                {"sheetId": 1765528573, "title": "ALL CLIENTS"},
            ]

    found = gsheets.SheetsClient.tab_named(
        Book(), "1uqe60", "1765528573",
    )
    assert found == "ALL CLIENTS"


def test_a_gid_that_names_no_tab_comes_back_empty():
    class Book:
        def tabs(self, sheet_id):
            return [{"sheetId": 0, "title": "Sheet1"}]

    assert gsheets.SheetsClient.tab_named(Book(), "x", "999") == ""


# ------------------------ telling one refresh token from another

# They all start "1//" and carry nothing readable, so a new one pasted and a
# new one forgotten look identical — and both look like the scope never having
# been ticked. Google names the scopes on every refresh.


def test_the_scopes_come_back_short(monkeypatch):
    import httpx

    from wilbyte import gsheets

    def answer(url, **kwargs):
        return httpx.Response(
            200,
            json={"access_token": "a", "expires_in": 3599, "scope": (
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/drive.file "
                "https://www.googleapis.com/auth/documents"
            )},
            request=httpx.Request("POST", "https://x"),
        )

    monkeypatch.setattr(httpx, "post", answer)

    assert gsheets.granted(gsheets.Credentials("i", "s", "1//old")) == [
        "gmail.readonly", "drive.file", "documents",
    ]


def test_a_token_without_the_new_scope_says_so(monkeypatch):
    import httpx

    from wilbyte import gsheets
    from wilbyte.bot import jobs
    from types import SimpleNamespace

    def answer(url, **kwargs):
        return httpx.Response(
            200,
            json={"scope": "https://www.googleapis.com/auth/gmail.readonly"},
            request=httpx.Request("POST", "https://x"),
        )

    monkeypatch.setattr(httpx, "post", answer)
    (ok, said), = jobs._check_google_scopes(SimpleNamespace(secrets=SimpleNamespace(
        google_client_id="i", google_client_secret="s", google_refresh_token="1//old",
        gmail_refresh_token="", gmail_client_id="", gmail_client_secret="",
    )))

    assert ok is False
    assert "gmail.readonly" in said
    assert "still the old token" in said


def test_a_token_with_it_passes(monkeypatch):
    import httpx

    from wilbyte import gsheets
    from wilbyte.bot import jobs
    from types import SimpleNamespace

    def answer(url, **kwargs):
        return httpx.Response(
            200,
            json={"scope": (
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/documents"
            )},
            request=httpx.Request("POST", "https://x"),
        )

    monkeypatch.setattr(httpx, "post", answer)
    (ok, said), = jobs._check_google_scopes(SimpleNamespace(secrets=SimpleNamespace(
        google_client_id="i", google_client_secret="s", google_refresh_token="1//new",
        gmail_refresh_token="", gmail_client_id="", gmail_client_secret="",
    )))

    assert ok is True
    assert "documents" in said
    assert "old token" not in said


def test_it_asks_about_the_token_the_docs_actually_use(monkeypatch):
    """GMAIL_REFRESH_TOKEN overrides the Sheets one for Gmail, Drive and Docs,
    so checking the Sheets one would answer about the wrong token."""
    import httpx

    from wilbyte import gsheets
    from wilbyte.bot import jobs
    from types import SimpleNamespace

    asked = {}

    def answer(url, **kwargs):
        asked.update(kwargs.get("data") or {})
        return httpx.Response(200, json={"scope": ""}, request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(httpx, "post", answer)
    jobs._check_google_scopes(SimpleNamespace(secrets=SimpleNamespace(
        google_client_id="sheets-id", google_client_secret="s",
        google_refresh_token="1//sheets",
        gmail_refresh_token="1//gmail", gmail_client_id="gmail-id",
        gmail_client_secret="gs",
    )))

    assert asked["refresh_token"] == "1//gmail"
    assert asked["client_id"] == "gmail-id"
