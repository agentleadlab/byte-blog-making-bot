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


# --------------------------------- the masterlist summary


from wilbyte import leadsheets
from wilbyte.leadsheets import Masterlist


def test_the_sheet_link_is_found_in_an_embed():
    """LeadLab posts each lead as an embed, so the link isn't in the text."""
    embed = {
        "title": "New Trucker TESTING Lead",
        "description": "Name: Salim Thomas\nState: PA\n[Access Sheet Here]"
        f"(https://docs.google.com/spreadsheets/d/{SHEET}/edit?usp=sharing)",
    }
    assert leadsheets.sheet_in_message("", [embed]).startswith(
        f"https://docs.google.com/spreadsheets/d/{SHEET}"
    )


def test_the_link_is_found_in_a_field_too():
    embed = {"fields": [{"name": "Sheet", "value":
             f"https://docs.google.com/spreadsheets/d/{SHEET}/edit"}]}
    assert SHEET in leadsheets.sheet_in_message("", [embed])


def test_a_channel_with_no_link_comes_back_empty():
    assert leadsheets.sheet_in_message("just chatting", [{"description": "no link"}]) == ""


@pytest.mark.parametrize(
    "channel,wanted",
    [
        ("🚚 otp-trucker-iul-masterlist", "OTP Trucker IUL"),
        ("otp-widow-vet-masterlist", "OTP Widow VET"),
        ("lp-iul-masterlist", "LP IUL"),
        ("annuity-masterlist", "Annuity"),
        ("ai-vets", "AI VETS"),
        ("spanish-fex-masterlist", "Spanish FEX"),
        ("lp-tfr-masterlist", "LP TFR"),
    ],
)
def test_a_channel_name_reads_as_the_lead_type(channel, wanted):
    """Not "Otp Iul" - the acronyms are the team's and they keep their case."""
    assert leadsheets.tidy_name(channel) == wanted


def test_the_inactive_category_goes_on_its_own_tab():
    found = [
        Masterlist(category="IUL Masterlist", name="LP IUL", count=12),
        Masterlist(category="INACTIVE Masterlist", name="Instant IUL", count=3),
    ]
    live, idle = leadsheets.split_by_state(found)

    assert [held.name for held in live] == ["LP IUL"]
    assert [held.name for held in idle] == ["Instant IUL"]


def test_the_summary_has_four_columns_and_a_header():
    rows = leadsheets.summary_rows([
        Masterlist(category="IUL Masterlist", name="LP IUL", sheet="u", count=1284),
    ])
    assert rows[0] == list(leadsheets.HEADER)
    assert rows[1] == ["IUL", "LP IUL", "u", 1284]


def test_a_count_that_could_not_be_read_is_a_dash_not_a_nought():
    """Nought is a number somebody will act on."""
    rows = leadsheets.summary_rows([Masterlist(category="IUL", name="LP IUL")])
    assert rows[1][3] == "—"


def test_the_total_ignores_the_ones_that_failed():
    found = [
        Masterlist(category="c", name="a", count=10),
        Masterlist(category="c", name="b"),
        Masterlist(category="c", name="c", count=5),
    ]
    assert leadsheets.total(found) == 15


def test_what_failed_is_named_in_the_report():
    live = [Masterlist(category="IUL", name="LP IUL", problem="no sheet link")]
    said = leadsheets.describe(live, [])
    assert "LP IUL" in said and "no sheet link" in said
