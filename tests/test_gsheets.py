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
