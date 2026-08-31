"""Reading and writing the leads Google Sheets."""

import httpx
import pytest

from wilbyte import gsheets
from wilbyte.gsheets import SheetsError, explain, missing_vars, sheet_id

SHEET = "1DMp9zsC23V8HMm6Nui7ff7hLoZ3ayjXZA3TTmN8c3TE"


def test_the_id_comes_out_of_a_pasted_link():
    assert sheet_id(
        f"https://docs.google.com/spreadsheets/d/{SHEET}/edit?gid=0#gid=0"
    ) == SHEET


def test_a_bare_id_is_accepted_as_itself():
    assert sheet_id(SHEET) == SHEET


def test_something_that_is_not_a_sheet_link_is_refused():
    with pytest.raises(SheetsError, match="doesn't look like a Google Sheet"):
        sheet_id("https://agentleadlab.com/")


def test_a_docs_link_is_not_a_sheet_link():
    """A Doc and a Sheet are different things behind similar URLs."""
    with pytest.raises(SheetsError):
        sheet_id("https://docs.google.com/document/d/" + SHEET + "/edit")


def test_the_leads_credentials_are_their_own(monkeypatch):
    """Captions are fetched as the channel's account; this is a different one,
    so one refresh token could never have done both."""
    for name in gsheets.OAUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    assert missing_vars() == list(gsheets.OAUTH_VARS)
    assert gsheets.configured() is False
    assert "GOOGLE_REFRESH_TOKEN" not in gsheets.OAUTH_VARS


def test_two_of_three_behaves_like_none(monkeypatch):
    monkeypatch.setenv("LEADS_GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("LEADS_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.delenv("LEADS_GOOGLE_REFRESH_TOKEN", raising=False)

    assert gsheets.configured() is False
    assert missing_vars() == ["LEADS_GOOGLE_REFRESH_TOKEN"]


def _response(status, text):
    return httpx.Response(
        status, text=text, request=httpx.Request("GET", "https://sheets.googleapis.com/")
    )


def test_a_missing_scope_is_named_as_such():
    said = explain(_response(403, '{"error":{"message":"insufficient scopes"}}'), SHEET)
    assert "doesn't cover Sheets" in said


def test_a_plain_403_reads_as_the_sheet_not_being_shared():
    assert "Share it" in explain(_response(403, "caller lacks permission"), SHEET)


def test_a_dead_refresh_token_says_how_to_mint_another():
    said = gsheets._explain_refresh(_response(400, '{"error":"invalid_grant"}'))
    assert "oauthplayground" in said


def test_a_mismatched_client_is_told_apart_from_a_dead_token():
    said = gsheets._explain_refresh(_response(401, '{"error":"invalid_client"}'))
    assert "CLIENT_ID" in said


def test_nothing_happens_without_credentials(monkeypatch):
    for name in gsheets.OAUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    gsheets._token_cache.clear()
    with pytest.raises(SheetsError, match="isn't set up"):
        gsheets.access_token()
