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


def test_a_plain_403_reads_as_the_sheet_not_being_shared(monkeypatch):
    monkeypatch.setattr(gsheets, "whoami", lambda: "")
    assert "share the sheet" in explain(_response(403, "caller lacks permission"), SHEET)


def test_the_403_names_the_account_when_it_can(monkeypatch):
    """The whole of a sharing problem is which account to share with."""
    monkeypatch.setattr(gsheets, "whoami", lambda: "franklin@agentleadlab.com")
    assert "franklin@agentleadlab.com" in explain(
        _response(403, "caller lacks permission"), SHEET
    )


def test_an_api_that_was_never_switched_on_is_not_a_sharing_problem():
    """Google answers 403 for both, and telling somebody to share a sheet they
    already own wastes their afternoon."""
    body = (
        '{"error":{"code":403,"message":"Google Sheets API has not been used in '
        'project 12345 before or it is disabled.","status":"PERMISSION_DENIED"}}'
    )
    said = explain(_response(403, body), SHEET)
    assert "not enabled" in said
    assert "sharing" in said.lower()


def test_an_unrecognised_403_shows_what_google_actually_said(monkeypatch):
    """Two wrong guesses in a row is what made this worth printing."""
    monkeypatch.setattr(gsheets, "whoami", lambda: "")
    said = explain(_response(403, "some new refusal nobody has seen"), SHEET)
    assert "some new refusal" in said


def test_asking_who_it_is_never_raises(monkeypatch):
    """It runs inside an error path; a second failure there helps nobody."""
    monkeypatch.setattr(gsheets, "access_token", lambda **k: (_ for _ in ()).throw(
        SheetsError("no credentials")
    ))
    assert gsheets.whoami() == ""


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
    assert rows[1] == ["IUL", "LP IUL", '=HYPERLINK("u","Open sheet")', 1284]


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


def test_one_refusal_is_said_once_however_many_sheets_it_hit():
    """Twenty sheets that aren't shared is one problem with twenty names."""
    refused = "Google refused: the account can't open that sheet"
    live = [
        Masterlist(category="IUL", name=f"Type {number}", problem=refused)
        for number in range(20)
    ]
    said = leadsheets.describe(live, [])

    assert said.count("Google refused") == 1
    assert "**20** not counted" in said
    assert "Type 0" in said


def test_two_different_failures_stay_two_lines():
    """Merging them would tell somebody to share a sheet that was only slow."""
    found = [
        Masterlist(category="IUL", name="LP IUL", problem="not shared"),
        Masterlist(category="IUL", name="Annuity", problem="HTTP 503"),
    ]
    said = leadsheets.describe(found, [])
    assert "not shared" in said and "HTTP 503" in said


def test_a_long_list_of_names_is_cut_short():
    live = [
        Masterlist(category="IUL", name=f"Type {number}", problem="not shared")
        for number in range(40)
    ]
    said = leadsheets.describe(live, [])
    assert "more" in said
    assert len(said) < 1200


def test_googles_own_words_do_not_repeat_under_every_group():
    """`explain` appends them; the heading is the part somebody acts on."""
    problem = "Share it with franklin@agentleadlab.com.\n-# Google said: {\"code\":403}"
    said = leadsheets.describe(
        [Masterlist(category="IUL", name="LP IUL", problem=problem)], []
    )
    assert "franklin@agentleadlab.com" in said
    assert "Google said" not in said


# --------------------------------- Google having a moment


def test_a_503_is_asked_again_rather_than_becoming_a_dash(monkeypatch):
    """It means "ask again", and a dash on the summary reads as a real problem."""
    monkeypatch.setattr(gsheets, "RETRY_PAUSES", (0, 0))
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    tries = []

    def flaky(url, **kwargs):
        tries.append(url)
        if len(tries) < 3:
            return _response(503, "The service is currently unavailable.")
        return httpx.Response(
            200,
            json={"values": [["Name", "a", "b"]]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(gsheets.httpx, "get", flaky)
    assert gsheets.row_count(SHEET) == 2
    assert len(tries) == 3


def test_a_403_is_not_retried(monkeypatch):
    """Permission does not improve by asking a second time."""
    monkeypatch.setattr(gsheets, "RETRY_PAUSES", (0, 0))
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    monkeypatch.setattr(gsheets, "whoami", lambda: "")
    tries = []

    def refused(url, **kwargs):
        tries.append(url)
        return _response(403, "caller lacks permission")

    monkeypatch.setattr(gsheets.httpx, "get", refused)
    with pytest.raises(SheetsError):
        gsheets.row_count(SHEET)
    assert len(tries) == 1


def test_giving_up_still_says_what_google_said(monkeypatch):
    monkeypatch.setattr(gsheets, "RETRY_PAUSES", (0,))
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    monkeypatch.setattr(
        gsheets.httpx, "get",
        lambda url, **kwargs: _response(503, "The service is currently unavailable."),
    )
    with pytest.raises(SheetsError, match="503"):
        gsheets.row_count(SHEET)


# --------------------------------- the Drive folder of masterlists


FOLDER = "1AbCdEfGhIjKlMnOpQrStUv"


def test_the_folder_id_comes_out_of_a_pasted_link():
    assert gsheets.folder_id(
        f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing"
    ) == FOLDER


def test_a_sheet_link_is_not_a_folder_link():
    with pytest.raises(SheetsError, match="Drive folder"):
        gsheets.folder_id(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit")


def test_reading_the_folder_needs_the_wide_drive_scope():
    """`drive.file` only ever sees files the app made itself, and the folder is
    full of files it didn't."""
    assert "https://www.googleapis.com/auth/drive" in gsheets.SCOPES


@pytest.mark.parametrize(
    "channel,file_name",
    [
        ("OTP Trucker IUL", "TRUCKER IUL Leads Masterlist"),
        ("Blue Collar", "Blue Collar Masterlist"),
        ("Spanish IUL LP", "Spanish IUL LP - Masterlist"),
        # The team's own reference file spells it this way.
        ("Trucker Leads", "TRUCKER LEADS Materlist"),
    ],
)
def test_a_channel_finds_its_file_however_it_is_spelled(channel, file_name):
    files = [{"id": "x", "name": file_name, "url": "u"}]
    assert leadsheets.find_sheet(channel, files) == files[0]


def test_a_near_miss_is_not_paired():
    """"Vet Leads Masterlist" is inside "OTP VET 2" as a word, and pairing them
    would put one masterlist's count on another masterlist's row."""
    files = [{"id": "x", "name": "Vet Leads Masterlist", "url": "u"}]
    assert leadsheets.find_sheet("OTP VET 2", files) is None


def test_two_equally_close_files_leave_it_alone():
    files = [
        {"id": "a", "name": "OTP IUL Masterlist", "url": "u"},
        {"id": "b", "name": "OTP IUL Spanish Masterlist", "url": "v"},
    ]
    assert leadsheets.find_sheet("OTP IUL Spanish Blue Collar", files) is None


def test_the_channel_link_wins_over_a_similar_file():
    """The lead system writes into the link it posts; a similarly named file is
    not a promise that they are the same sheet."""
    found = [Masterlist(category="IUL", name="LP IUL", sheet="https://posted")]
    files = [{"id": "x", "name": "LP IUL Masterlist", "url": "https://folder"}]

    combined = leadsheets.combine(found, files)
    assert combined[0].sheet == "https://posted"
    # ...and the file is still counted as claimed, not listed a second time.
    assert len(combined) == 1


def test_the_folder_fills_in_a_channel_with_no_link():
    found = [Masterlist(category="IUL", name="LP IUL")]
    files = [{"id": "x", "name": "LP IUL Masterlist", "url": "https://folder"}]

    combined = leadsheets.combine(found, files)
    assert combined[0].sheet == "https://folder"
    assert combined[0].problem == ""


def test_a_file_no_channel_claimed_is_still_on_the_masterfile():
    combined = leadsheets.combine(
        [], [{"id": "x", "name": "Annuity Leads Masterlist", "url": "u"}]
    )
    assert [held.name for held in combined] == ["Annuity"]
    assert combined[0].category == leadsheets.DRIVE_ONLY


def test_a_lead_type_with_no_sheet_anywhere_is_what_gets_made():
    found = [
        Masterlist(category="IUL", name="LP IUL", sheet="u"),
        Masterlist(category="IUL", name="Nova"),
    ]
    combined = leadsheets.combine(found, [])
    assert [held.name for held in leadsheets.missing(combined)] == ["Nova"]


def test_nothing_is_created_without_being_asked():
    """Eight quiet channels must not become eight spreadsheets in a Drive."""
    from wilbyte.bot import client

    assert client._MAKE_THEM.search("masterlists") is None
    assert client._MAKE_THEM.search("create the missing ones")


def test_a_new_sheet_is_named_after_the_lead_type():
    assert leadsheets.new_sheet_title("OTP Trucker IUL") == "OTP Trucker IUL Masterlist"
    assert leadsheets.new_sheet_title("Nova Masterlist") == "Nova Masterlist"


def test_making_one_files_it_in_the_folder_and_writes_a_header(monkeypatch):
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    sent = {}
    written = {}

    def post(url, **kwargs):
        sent["url"] = url
        sent["json"] = kwargs.get("json")
        return httpx.Response(
            200, json={"id": SHEET, "name": "Nova Masterlist"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gsheets.httpx, "post", post)
    monkeypatch.setattr(
        gsheets, "write_rows", lambda sheet, rows, **k: written.update(rows=rows) or "u"
    )

    made = gsheets.create_sheet("Nova Masterlist", folder=FOLDER, header=["Name"])

    assert sent["json"]["parents"] == [FOLDER]
    assert sent["json"]["mimeType"] == gsheets.SHEET_MIME
    assert made["url"].endswith(f"{SHEET}/edit")
    assert written["rows"] == [["Name"]]


def test_the_new_sheet_shows_up_as_made_on_the_report():
    live = [Masterlist(category="IUL", name="Nova", sheet="u", count=0, created=True)]
    assert "Made in the folder: Nova" in leadsheets.describe(live, [])


def test_a_lead_type_with_no_sheet_is_told_apart_from_one_that_failed():
    found = [
        Masterlist(category="IUL", name="Nova"),
        Masterlist(category="IUL", name="LP IUL", sheet="u", problem="not shared"),
    ]
    said = leadsheets.describe(found, [])
    assert "no sheet anywhere: Nova" in said
    assert "masterlists create" in said
    assert "not shared" in said


# --------------------------------- never writing on a masterlist


def test_a_tab_with_somebody_elses_data_is_not_cleared(monkeypatch):
    """The whole promise is that the masterlists are read and never written.
    A wrong id in the .env is the only way that promise breaks, so it refuses."""
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    monkeypatch.setattr(
        gsheets, "first_row", lambda sheet, tab: ["Name", "Email", "Phone"]
    )
    hit = []
    monkeypatch.setattr(
        gsheets.httpx, "post", lambda *a, **k: hit.append(a) or _response(200, "{}")
    )

    with pytest.raises(SheetsError, match="won't write there"):
        gsheets.write_rows(SHEET, [["x"]], tab="Active",
                           expect_header=list(leadsheets.HEADER))
    assert hit == []  # nothing was cleared


def test_the_summary_tab_it_wrote_last_time_is_replaced(monkeypatch):
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    monkeypatch.setattr(gsheets, "first_row", lambda sheet, tab: list(leadsheets.HEADER))
    monkeypatch.setattr(
        gsheets.httpx, "post", lambda *a, **k: _response(200, "{}")
    )
    monkeypatch.setattr(
        gsheets.httpx, "put",
        lambda url, **k: httpx.Response(200, json={}, request=httpx.Request("PUT", url)),
    )

    url = gsheets.write_rows(
        SHEET, [list(leadsheets.HEADER)], tab="Active",
        expect_header=list(leadsheets.HEADER),
    )
    assert SHEET in url


def test_an_empty_tab_is_fine_to_write():
    """The first run has nothing on the tab, and that is not a warning sign."""
    import inspect
    source = inspect.getsource(gsheets.write_rows)
    assert "if found and" in source


def test_the_summary_refuses_to_be_one_of_the_masterlists():
    from wilbyte.bot import client

    files = [{"id": SHEET, "name": "OTP Trucker IUL Masterlist", "url": "u"}]
    with pytest.raises(SheetsError, match="one of the masterlists"):
        client._write_lead_summary(SHEET, [], [], files=files)


def test_nothing_in_the_sheets_code_ever_deletes():
    """Counting leads is a read. There is no call here that could remove one."""
    from pathlib import Path

    source = Path(gsheets.__file__).read_text()
    assert "httpx.delete" not in source
    assert "deleteSheet" not in source
    assert "DELETE" not in source


# --------------------------------- moving one between tabs


from wilbyte import leadstate


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("move the otp trucker iul masterlist to inactive", ("otp trucker iul", "inactive")),
        ("move otp widow vet to active", ("otp widow vet", "active")),
        ("move spanish fex masterlist to in active", ("spanish fex", "inactive")),
        ("mark lp iul masterlist as inactive", ("lp iul", "inactive")),
    ],
)
def test_which_masterlist_and_which_way(said, wanted):
    assert leadstate.move_asked(said) == wanted


def test_a_move_that_does_not_say_which_way_is_refused():
    """Guessing is how a live lead type disappears off the list."""
    assert leadstate.move_asked("move the trucker masterlist") is None


def test_every_way_of_writing_the_name_lands_on_one_entry():
    keys = {
        leadstate.key(name) for name in (
            "otp-trucker-iul-masterlist",
            "🚚 otp-trucker-iul-masterlist",
            "OTP Trucker IUL",
            "the otp trucker iul masterlist",
        )
    }
    assert len(keys) == 1


def test_what_was_said_beats_the_discord_category(tmp_path):
    """RYTE only has read permission in that server, so an override is kept
    here rather than the channel being moved."""
    live_by_category = Masterlist(category="IUL Masterlist", name="OTP Trucker IUL")
    idle_by_category = Masterlist(category="INACTIVE Masterlist", name="Instant IUL")

    said = {leadstate.key("OTP Trucker IUL"): "inactive",
            leadstate.key("Instant IUL"): "active"}
    live, idle = leadsheets.split_by_state([live_by_category, idle_by_category], said=said)

    assert [held.name for held in live] == ["Instant IUL"]
    assert [held.name for held in idle] == ["OTP Trucker IUL"]


def test_the_category_still_decides_when_nobody_has_said():
    found = [Masterlist(category="INACTIVE Masterlist", name="Instant IUL")]
    live, idle = leadsheets.split_by_state(found, said={})
    assert [held.name for held in idle] == ["Instant IUL"]


def test_an_override_survives_being_written_and_read(tmp_path):
    path = tmp_path / "masterlist-state.json"
    leadstate.set_state("OTP Trucker IUL", "inactive", path)

    assert leadstate.load(path) == {"otp trucker iul": "inactive"}
    assert leadstate.state_of("otp-trucker-iul-masterlist", held=leadstate.load(path)) == "inactive"

    leadstate.clear("otp trucker iul", path)
    assert leadstate.load(path) == {}


def test_the_sheet_column_is_a_link_not_a_url():
    rows = leadsheets.summary_rows([
        Masterlist(category="IUL", name="LP IUL", sheet="https://x", count=3)
    ])
    assert rows[1][2] == '=HYPERLINK("https://x","Open sheet")'


# --------------------------------- counting eighty-odd sheets


def test_the_counts_run_a_few_at_a_time(monkeypatch):
    """One after another is four minutes of "is typing…"; all at once trips
    Google's per-minute read limit and comes back full of dashes."""
    import asyncio as aio
    from wilbyte.bot import client

    assert 1 < client.AT_ONCE <= 10

    live = [Masterlist(category="c", name=f"n{i}", sheet="u") for i in range(20)]
    running, most = 0, 0

    def count(sheet, **kwargs):
        nonlocal running, most
        running += 1
        most = max(most, running)
        try:
            return 7
        finally:
            running -= 1

    monkeypatch.setattr(gsheets, "row_count", count)
    aio.run(client._count_leads(live))

    assert [held.count for held in live] == [7] * 20
    assert most <= client.AT_ONCE


def test_one_sheet_refusing_does_not_stop_the_others(monkeypatch):
    import asyncio as aio
    from wilbyte.bot import client

    live = [
        Masterlist(category="c", name="good", sheet="u"),
        Masterlist(category="c", name="bad", sheet="v"),
    ]

    def count(sheet, **kwargs):
        if sheet == "v":
            raise SheetsError("not shared")
        return 3

    monkeypatch.setattr(gsheets, "row_count", count)
    aio.run(client._count_leads(live))

    assert live[0].count == 3
    assert live[1].count is None and "not shared" in live[1].problem
