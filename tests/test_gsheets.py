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


def test_the_summary_has_a_row_per_lead_type_and_a_header():
    rows = leadsheets.summary_rows([
        Masterlist(category="IUL Masterlist", name="LP IUL", sheet="u",
                   channel="https://discord.com/channels/1/2", count=1284),
    ])
    assert rows[0] == list(leadsheets.HEADER)
    assert rows[1] == [
        "LP IUL",
        '=HYPERLINK("https://discord.com/channels/1/2","Open channel")',
        '=HYPERLINK("u","Open sheet")', 1284,
    ]


def test_the_category_is_not_a_column():
    """It repeated the lead type's own name - "IUL" beside "OTP Blue Collar
    IUL" - and the tab already says whether it is live."""
    assert "Category" not in leadsheets.HEADER
    rows = leadsheets.summary_rows([
        Masterlist(category="IUL Masterlist", name="LP IUL", sheet="u", count=3)
    ])
    assert rows[1][0] == "LP IUL"


def test_a_lead_type_with_no_channel_shows_a_dash_not_a_broken_link():
    rows = leadsheets.summary_rows([Masterlist(category="IUL", name="LP IUL", sheet="u")])
    assert rows[1][1] == "—"


def test_a_count_that_could_not_be_read_is_a_dash_not_a_nought():
    """Nought is a number somebody will act on."""
    rows = leadsheets.summary_rows([Masterlist(category="IUL", name="LP IUL")])
    assert rows[1][3] == "—"   # the count
    assert rows[1][2] == "—"   # and the sheet


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
    assert leadsheets.new_sheet_title("OTP Trucker IUL", year=2026) == (
        "OTP Trucker IUL Masterlist 2026"
    )


def test_making_one_files_it_in_the_folder_and_writes_a_header(monkeypatch):
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    sent = {}
    written = {}

    def sent_write(method, url, **kwargs):
        sent["url"] = url
        sent["json"] = kwargs.get("json")
        return httpx.Response(
            200, json={"id": SHEET, "name": "Nova Masterlist"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(gsheets.httpx, "request", sent_write)
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
        gsheets, "first_row", lambda sheet, tab: ["Name", "Email", "Phone", "State"]
    )
    hit = []
    monkeypatch.setattr(
        gsheets.httpx, "request", lambda *a, **k: hit.append(a) or _response(200, "{}")
    )

    with pytest.raises(SheetsError, match="won't write there"):
        gsheets.write_rows(SHEET, [["x"]], tab="Active",
                           expect_header=list(leadsheets.HEADER))
    assert hit == []  # nothing was cleared


def test_the_summary_tab_it_wrote_last_time_is_replaced(monkeypatch):
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    monkeypatch.setattr(gsheets, "first_row", lambda sheet, tab: list(leadsheets.HEADER))
    monkeypatch.setattr(
        gsheets.httpx, "request",
        lambda method, url, **k: httpx.Response(
            200, json={}, request=httpx.Request(method, url)
        ),
    )

    url = gsheets.write_rows(
        SHEET, [list(leadsheets.HEADER)], tab="Active",
        expect_header=list(leadsheets.HEADER),
    )
    assert SHEET in url


def test_an_empty_tab_is_fine_to_write():
    """The first run has nothing on the tab, and that is not a warning sign."""
    assert gsheets.ours([], list(leadsheets.HEADER)) is True
    assert gsheets.ours(["", ""], list(leadsheets.HEADER)) is True


def test_a_column_deleted_by_hand_does_not_block_the_update():
    """Somebody tidied the sheet, which is not a reason to stop updating it."""
    assert gsheets.ours(
        ["Type of leads", "Sheet", "Total leads", ""], list(leadsheets.HEADER)
    ) is True


def test_a_lead_sheet_is_still_refused():
    assert gsheets.ours(
        ["Name", "Email", "Phone", "State"], list(leadsheets.HEADER)
    ) is False
    assert gsheets.ours(
        ["Agent_Name", "Agent_Spend", "States", "Lead Cap"], list(leadsheets.HEADER)
    ) is False


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
            return 7, ["Name", "Email"]
        finally:
            running -= 1

    monkeypatch.setattr(gsheets, "count_and_header", count)
    monkeypatch.setattr(gsheets, "facts", lambda sheet: ("LP IUL Masterlist", []))
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
        return 3, ["Name", "Email"]

    monkeypatch.setattr(gsheets, "count_and_header", count)
    monkeypatch.setattr(gsheets, "facts", lambda sheet: ("LP IUL Masterlist", []))
    aio.run(client._count_leads(live))

    assert live[0].count == 3
    assert live[1].count is None and "not shared" in live[1].problem


def test_the_masterfile_is_not_one_of_the_masterlists(monkeypatch):
    """It is filed in the same folder as the sheets it summarises, which is
    tidy and nearly cost it the ability to write to itself."""
    import inspect
    from wilbyte.bot import client

    source = inspect.getsource(client._send_lead_summary)
    assert 'sheet_id(into)' in source and "file for file in files" in source


def test_every_other_file_in_the_folder_is_still_refused():
    from wilbyte.bot import client

    files = [{"id": SHEET, "name": "Blue Collar Masterlist", "url": "u"}]
    with pytest.raises(SheetsError, match="one of the masterlists"):
        client._write_lead_summary(SHEET, [], [], files=files)


# --------------------------------- the masterfile is the only writable sheet


def test_a_masterlist_cannot_be_written_to_at_all(monkeypatch):
    """Not "shouldn't" - can't. Every lead sheet is read-only to RYTE."""
    monkeypatch.setenv("LEADS_SUMMARY_SHEET_ID", SHEET)
    gsheets._just_made.clear()
    other = "1kwsuiPD_8p4B_jGG8_-1RL9bren5KLC_mRHTz1SrN2k"

    for call in (
        lambda: gsheets.write_rows(other, [["x"]], tab="Sheet1"),
        lambda: gsheets.ensure_tab(other, "Active"),
        lambda: gsheets.prettify(other, "Active", rows=2),
    ):
        with pytest.raises(SheetsError, match="not allowed to change"):
            call()


def test_the_masterfile_named_in_the_env_is_writable(monkeypatch):
    monkeypatch.setenv("LEADS_SUMMARY_SHEET_ID", SHEET)
    assert gsheets.writable(SHEET) is True
    assert gsheets.writable(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit")


def test_nothing_is_writable_when_the_env_names_nothing(monkeypatch):
    """A blank .env must not read as "anything goes"."""
    monkeypatch.delenv("LEADS_SUMMARY_SHEET_ID", raising=False)
    gsheets._just_made.clear()
    assert gsheets.writable(SHEET) is False


def test_a_sheet_ryte_just_made_may_have_its_header_written(monkeypatch):
    monkeypatch.delenv("LEADS_SUMMARY_SHEET_ID", raising=False)
    monkeypatch.setattr(gsheets, "access_token", lambda **k: "token")
    gsheets._just_made.clear()
    wrote = {}

    monkeypatch.setattr(
        gsheets.httpx, "request",
        lambda method, url, **k: httpx.Response(
            200, json={"id": SHEET, "name": "Nova Masterlist"},
            request=httpx.Request(method, url),
        ),
    )
    monkeypatch.setattr(
        gsheets, "write_rows", lambda sheet, rows, **k: wrote.update(sheet=sheet) or "u"
    )

    gsheets.create_sheet("Nova Masterlist", folder=FOLDER, header=["Name"])
    assert SHEET in gsheets._just_made
    assert wrote["sheet"] == SHEET


# --------------------------------- staying under Google's minute


def test_the_reads_pace_themselves_under_the_limit(monkeypatch):
    """Eighty-five sheets against a 60-a-minute limit is how twenty of them
    came back as dashes."""
    monkeypatch.setattr(gsheets, "READS_A_MINUTE", 3)
    gsheets._recent.clear()
    waited = []
    monkeypatch.setattr(gsheets.time, "sleep", lambda seconds: waited.append(seconds))

    for _ in range(3):
        gsheets._pace()
    assert waited == []

    # The fourth has to wait for the first to fall out of the minute.
    monkeypatch.setattr(gsheets.time, "sleep", lambda seconds: gsheets._recent.clear())
    gsheets._pace()
    assert len(gsheets._recent) == 1


def test_a_quota_refusal_waits_far_longer_than_a_wobble():
    """The limit is per minute; three seconds of politeness just spends another
    request on the same refusal."""
    quota = gsheets._pauses_for(_response(429, "Quota exceeded"))
    wobble = gsheets._pauses_for(_response(503, "unavailable"))

    assert sum(quota) >= 60
    assert sum(quota) > sum(wobble)


def test_the_quota_message_is_not_four_lines_of_json():
    said = explain(
        _response(429, '{"error":{"code":429,"message":"Quota exceeded for quota '
                       "metric 'Read requests' and limit 'Read requests per minute "
                       "per user' of service 'sheets.googleapis.com' for consumer "
                       "'project_number:965472774442'.\"}}"),
        SHEET,
    )
    assert "per-minute read limit" in said
    assert "quota metric" not in said
    assert len(said) < 300


# --------------------------------- Discord decides what is a lead type


@pytest.mark.parametrize(
    "category,wanted",
    [
        ("IUL Masterlist", True),
        ("INACTIVE Masterlist", True),
        ("AI Leads", True),
        ("Text Channels", False),
        ("(no category)", False),
        ("", False),
    ],
)
def test_only_a_real_category_counts_as_a_lead_type(category, wanted):
    """A channel filed under no category is a channel somebody made."""
    assert leadsheets.worth_listing(category) is wanted


def test_the_drive_only_sheets_go_on_their_own_tab():
    """They are real sheets, but they are not the team's lead types and they
    do not belong on the tab somebody opens to ask what is live."""
    found = leadsheets.combine(
        [Masterlist(category="IUL Masterlist", name="LP IUL", sheet="u")],
        [{"id": "x", "name": "Jan 30 FEX", "url": "v"}],
    )
    theirs, others = leadsheets.apart(found)

    assert [held.name for held in theirs] == ["LP IUL"]
    assert [held.name for held in others] == ["Jan 30 FEX"]


def test_the_third_tab_is_only_written_when_there_is_something_for_it(monkeypatch):
    from wilbyte.bot import client

    made = []
    monkeypatch.setattr(gsheets, "ensure_tab", lambda into, tab: made.append(tab))
    monkeypatch.setattr(gsheets, "write_rows", lambda *a, **k: "u")
    monkeypatch.setattr(gsheets, "prettify", lambda *a, **k: None)
    monkeypatch.setattr(gsheets, "sheet_id", lambda x: x)

    client._write_lead_summary("into", [], [], [])
    assert leadsheets.OTHER_TAB not in made

    made.clear()
    client._write_lead_summary("into", [], [], [Masterlist(category="x", name="y")])
    assert leadsheets.OTHER_TAB in made


# --------------------------------- auto-deploy sheets are not lead sheets


def test_a_deploy_sheet_is_known_by_its_name():
    assert leadsheets.kind_of("Mortgage Protection [New] - Auto Deploy") == leadsheets.DEPLOY
    assert leadsheets.kind_of("Annuity Leads Masterlist") == leadsheets.LEADS


def test_a_deploy_sheet_is_known_by_its_columns_too():
    """Nobody is obliged to put "Auto Deploy" in the file name."""
    header = ["Agent_Name", "Agent_Spend", "States", "Active", "Lead Cap"]
    assert leadsheets.kind_of("Mortgage Protection V3", header) == leadsheets.DEPLOY


def test_a_real_masterlist_is_not_moved_on_one_loose_match():
    """"Daily Cap" alone could turn up on somebody's lead sheet, and moving a
    masterlist onto the wrong tab is worse than leaving a deploy sheet among
    them."""
    header = ["Name", "Email", "Phone", "State", "Daily Cap"]
    assert leadsheets.kind_of("Annuity Leads Masterlist", header) == leadsheets.LEADS


def test_a_deploy_sheet_is_never_adopted_as_a_channels_masterlist():
    """It reads as the Mortgage Protection channel's sheet on name alone, and
    adopting it would put an agent count in a lead column."""
    found = [Masterlist(category="MTG Masterlist", name="Mortgage Protection")]
    files = [{"id": "d", "name": "Mortgage Protection [New] - Auto Deploy", "url": "u"}]

    combined = leadsheets.combine(found, files)
    lists, deploys = leadsheets.by_kind(combined)

    assert lists[0].sheet == ""          # the channel still has no masterlist
    assert len(deploys) == 1
    assert deploys[0].sheet == "u"


def test_the_deploy_tab_says_which_lead_type_it_belongs_to():
    found = [Masterlist(category="MTG Masterlist", name="Mortgage Protection",
                        channel="https://discord.com/channels/1/2")]
    files = [{"id": "d", "name": "Mortgage Protection [New] - Auto Deploy", "url": "u"}]

    _, deploys = leadsheets.by_kind(leadsheets.combine(found, files))
    rows = leadsheets.deploy_rows(deploys)

    assert rows[0] == list(leadsheets.DEPLOY_HEADER)
    assert rows[1][0] == "Mortgage Protection"
    # The fix happens in Discord, so the row goes straight there.
    assert rows[1][1] == '=HYPERLINK("https://discord.com/channels/1/2","Open channel")'
    assert rows[1][4] == "—"


def test_a_deploy_sheet_with_no_channel_still_gets_a_row():
    _, deploys = leadsheets.by_kind(
        leadsheets.combine([], [{"id": "d", "name": "Rise Legacy Auto Deploy", "url": "u"}])
    )
    assert leadsheets.deploy_rows(deploys)[1][0] == leadsheets.NO_CHANNEL


def test_the_count_and_the_header_come_back_in_one_request(monkeypatch):
    """Asking twice would double a run that already paces for a per-minute
    limit."""
    asked = []

    def get(path, **params):
        asked.append(params)
        return {"valueRanges": [
            {"values": [["Name"], ["a"], ["b"]]},
            {"values": [["Name", "Email", "Phone"]]},
        ]}

    monkeypatch.setattr(gsheets, "_get", get)
    count, header = gsheets.count_and_header(SHEET)

    assert (count, header) == (2, ["Name", "Email", "Phone"])
    assert len(asked) == 1


def test_a_masterlist_that_records_the_agent_is_still_a_masterlist():
    """Twenty-two real lead types were moved onto the deploy tab because their
    sheets note which agent got the lead. Leads are what settles it."""
    header = ["Name", "Email", "Phone", "State", "Agent_Name", "Lead Received"]
    assert leadsheets.kind_of("OTP Trucker IUL Masterlist", header) == leadsheets.LEADS


def test_a_real_deploy_sheet_has_no_leads_on_it():
    header = ["Agent_Name", "Agent_Spend", "States", "Active", "Lead Cap",
              "Daily Cap", "Launch_Date"]
    assert leadsheets.kind_of("Mortgage Protection V3", header) == leadsheets.DEPLOY


def test_the_name_still_settles_it_whatever_the_columns_say():
    """A file called Auto Deploy is one, even if somebody put an email column
    on it."""
    header = ["Agent_Name", "Email", "Lead Cap"]
    assert leadsheets.kind_of("MTG [New] - Auto Deploy", header) == leadsheets.DEPLOY


def test_a_pinned_sheet_link_is_what_the_channel_means_now(monkeypatch):
    """Fixing a channel's sheet means pinning the right one; the lead posts
    underneath still carry the old link for as long as they sit there."""
    import inspect
    from wilbyte.bot import client

    source = inspect.getsource(client._lead_masterlists)
    assert "channel.pins()" in source
    assert source.index("channel.pins()") < source.index("channel.history(")


# --------------------------------- building the masterlist itself


def test_a_new_masterlist_is_named_so_nobody_mistakes_it():
    """There are near-duplicate masterlists in that folder going back years."""
    assert leadsheets.new_sheet_title("Mortgage Protection", year=2026) == (
        "Mortgage Protection Masterlist 2026"
    )
    assert leadsheets.new_sheet_title("Nova Masterlist", year=2026) == (
        "Nova Masterlist 2026"
    )


def test_the_year_in_the_name_does_not_break_the_matching():
    """Next run has to see it as that lead type's masterlist, not a new file."""
    files = [{"id": "x", "name": "Mortgage Protection Masterlist 2026", "url": "u"}]
    assert leadsheets.find_sheet("Mortgage Protection", files) == files[0]


def test_the_leads_tab_of_a_deploy_sheet_is_found():
    assert leadsheets.leads_tab_in(
        ["Agent_Config", "Available_Leads", "Assigned_Leads", "Agent_Jack_Duval"]
    ) == "Available_Leads"
    assert leadsheets.leads_tab_in(["Agent_Config", "Sheet1"]) == ""


def test_a_channel_linking_a_deploy_sheet_counts_as_having_no_masterlist():
    """Otherwise it keeps reporting an agent count as leads for as long as the
    old posts sit in the channel."""
    found = [Masterlist(
        category="MTG Masterlist", name="Mortgage Protection",
        sheet="https://docs.google.com/spreadsheets/d/" + SHEET + "/edit",
    )]
    files = [{"id": SHEET, "name": "Text-Verified MTG - Auto Deploy (New Setup)",
              "url": "u"}]

    lists, deploys = leadsheets.by_kind(leadsheets.combine(found, files))

    assert lists[0].sheet == ""
    assert leadsheets.missing(lists) == lists
    # ...and the deploy sheet is filed under the channel that linked it, not
    # under whatever its name happens to resemble.
    assert deploys[0].category == "Mortgage Protection"


def test_the_new_masterlist_takes_the_place_of_the_deploy_link():
    found = [Masterlist(
        category="MTG Masterlist", name="Mortgage Protection",
        sheet="https://docs.google.com/spreadsheets/d/" + SHEET + "/edit",
    )]
    files = [
        {"id": SHEET, "name": "MTG - Auto Deploy", "url": "u"},
        {"id": "new", "name": "Mortgage Protection Masterlist 2026", "url": "made"},
    ]
    lists, _ = leadsheets.by_kind(leadsheets.combine(found, files))
    assert lists[0].sheet == "made"


def test_a_sorted_out_row_says_so_and_is_highlighted():
    deploys = [
        Masterlist(category="Mortgage Protection", name="MTG Auto Deploy", sheet="u",
                   kind=leadsheets.DEPLOY,
                   status=f"{leadsheets.SORTED_OUT} — Mortgage Protection Masterlist 2026"),
        Masterlist(category="OTP FEX", name="FEX Auto Deploy", sheet="v",
                   kind=leadsheets.DEPLOY),
    ]
    rows = leadsheets.deploy_rows(deploys)

    assert rows[0][-1] == "Status"
    assert rows[1][-1].startswith(leadsheets.SORTED_OUT)
    assert rows[2][-1] == "Needs a masterlist"
    assert leadsheets.done_rows(rows) == [1]


def test_the_header_never_counts_as_a_sorted_out_row():
    rows = leadsheets.deploy_rows([])
    assert leadsheets.done_rows(rows) == []


def test_the_leads_tab_is_found_whatever_it_is_called():
    """Available_Leads on one deploy sheet, Master_Leads on the next."""
    assert leadsheets.leads_tab_in(
        ["Dashboard", "Agent_Config", "Master_Leads", "Agent_Maria_Garcia"]
    ) == "Master_Leads"


def test_the_agent_config_tab_is_the_surest_tell():
    """A deploy sheet kept outside the folder, named nothing in particular,
    still has to configure its agents somewhere."""
    tabs = ["Dashboard", "Agent_Config", "Master_Leads", "Agent_Luis_Orbezua"]
    assert leadsheets.config_tab_in(tabs) == "Agent_Config"
    assert leadsheets.config_tab_in(["Sheet1", "Notes"]) == ""


# --------------------------------- the sheet somebody named


from wilbyte import leadstate as _leadstate


def test_the_six_sheets_franklin_named_are_known():
    """Nearly every channel links a deploy sheet; these are the exceptions."""
    assert "1wkwoLkzMfhlmkNSLdSEo5YJKTP0Ax06foBYr8z_8Fqo" in (
        leadsheets.known_sheet("OTP Trucker IUL")
    )
    assert "1_14LZh3zTNXBLGgDs3JOwWExpEHWSEigoBUfGOlDRmQ" in (
        leadsheets.known_sheet("OTP VET 2")
    )
    assert leadsheets.known_sheet("otp-trucker-iul-masterlist") == (
        leadsheets.known_sheet("OTP IUL Truckers")
    )
    assert leadsheets.known_sheet("Spanish FEX") == ""


def test_a_named_sheet_beats_what_the_channel_links():
    found = [Masterlist(category="IUL Masterlist", name="OTP Trucker IUL",
                        sheet="https://docs.google.com/spreadsheets/d/" + SHEET + "/edit")]
    combined = leadsheets.combine(found, [])

    assert "1wkwoLkzMfhlmkNSLdSEo5YJKTP0Ax06foBYr8z_8Fqo" in combined[0].sheet
    assert combined[0].status == leadsheets.PINNED


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("masterlist OTP FEX https://docs.google.com/spreadsheets/d/" + SHEET,
         "otp fex"),
        ("https://docs.google.com/spreadsheets/d/" + SHEET + " for Spanish FEX",
         "spanish fex"),
        ("OTP VET 2 sheet is https://docs.google.com/spreadsheets/d/" + SHEET,
         "otp vet 2"),
    ],
)
def test_which_lead_type_and_which_sheet(said, wanted):
    found = _leadstate.sheet_asked(said)
    assert found is not None and found[0] == wanted
    assert SHEET in found[1]


def test_a_link_with_no_lead_type_is_not_an_instruction():
    """It says nothing about which lead type it belongs to."""
    assert _leadstate.sheet_asked(
        "https://docs.google.com/spreadsheets/d/" + SHEET
    ) is None
    assert _leadstate.sheet_asked("masterlists") is None


def test_a_pinned_sheet_survives_being_written_and_read(tmp_path):
    path = tmp_path / "masterlist-sheets.json"
    _leadstate.set_sheet("OTP FEX", "https://x", path)

    assert _leadstate.sheets(path) == {"otp fex": "https://x"}
    assert _leadstate.sheet_of("otp-fex-masterlist", held=_leadstate.sheets(path)) == (
        "https://x"
    )


def test_what_was_typed_beats_the_list_in_the_source(tmp_path):
    said = {"otp trucker iul": "https://typed"}
    assert leadsheets.pinned_sheet("OTP Trucker IUL", said=said) == "https://typed"


# --------------------------------- counting the right tab


def test_a_deploy_sheet_is_caught_by_its_tabs_even_outside_the_folder():
    """Spanish FEX's is called "Text-Verified Spanish FEX - Auto Deploy (No
    Reset Day)" and lives elsewhere; its Dashboard tab of state names was
    counted as fifty-six leads."""
    tabs = ["Dashboard", "Agent_Config", "Master_Leads", "Agent_Maria_Garcia"]
    assert leadsheets.kind_of("Some sheet nobody named well", tabs=tabs) == (
        leadsheets.DEPLOY
    )


def test_the_count_comes_off_the_named_tab(monkeypatch):
    asked = {}

    def get(path, **params):
        asked.update(params)
        return {"valueRanges": [{"values": [["Name"], ["a"]]}, {"values": [["Name"]]}]}

    monkeypatch.setattr(gsheets, "_get", get)
    gsheets.count_and_header(SHEET, tab="Master_Leads")

    assert asked["ranges"] == ["'Master_Leads'!A:A", "'Master_Leads'!1:1"]


def test_a_channel_linking_a_deploy_sheet_is_caught_during_counting(monkeypatch):
    """The one that mattered: Spanish FEX counted its Dashboard tab of state
    names and reported fifty-six leads."""
    import asyncio as aio
    from wilbyte.bot import client

    live = [Masterlist(category="FEX Masterlist", name="Spanish FEX", sheet="u",
                       channel="https://discord.com/channels/1/2")]

    monkeypatch.setattr(gsheets, "facts", lambda sheet: (
        "Text-Verified Spanish FEX - Auto Deploy (No Reset Day)",
        ["Dashboard", "Agent_Config", "Master_Leads", "Agent_Maria_Garcia"],
    ))
    monkeypatch.setattr(gsheets, "count_and_header", lambda sheet, **k: (6, ["Agent_Name"]))
    aio.run(client._count_leads(live))

    # The lead type is left with no masterlist, which is the truth...
    assert live[0].sheet == "" and live[0].count is None
    # ...and the deploy sheet is on the flagged list under that lead type.
    flagged = [held for held in live if held.kind == leadsheets.DEPLOY]
    assert len(flagged) == 1
    assert flagged[0].category == "Spanish FEX"
    assert flagged[0].channel == "https://discord.com/channels/1/2"


# --------------------------------- how many to make, and which


def _blank(*names):
    return [Masterlist(category="IUL Masterlist", name=name) for name in names]


def test_create_on_its_own_makes_them_all():
    from wilbyte.bot import client
    blank = _blank("Mortgage Protection", "OTP FEX", "Spanish FEX")
    assert client._who_to_make("create", blank) == blank


def test_create_one_makes_a_single_sheet_to_look_at():
    """Thirty sheets in somebody's Drive is not the thing to get wrong."""
    from wilbyte.bot import client
    blank = _blank("Mortgage Protection", "OTP FEX")
    made = client._who_to_make("create one", blank)
    assert [held.name for held in made] == ["Mortgage Protection"]


def test_a_named_lead_type_is_the_one_that_gets_made():
    from wilbyte.bot import client
    blank = _blank("Mortgage Protection", "OTP FEX", "Spanish FEX")
    made = client._who_to_make("create otp fex", blank)
    assert [held.name for held in made] == ["OTP FEX"]


def test_a_name_nobody_recognises_does_not_silently_make_everything():
    """It falls back to all only when no name was given at all."""
    from wilbyte.bot import client
    blank = _blank("Mortgage Protection")
    assert client._who_to_make("create one nonsense name", blank) == blank[:1]


# --------------------------------- naming it what it actually is


@pytest.mark.parametrize(
    "lead_type,deploy,wanted",
    [
        # The channel says one thing, the deploy sheet says which one it is.
        ("Mortgage Protection", "Text Verified MTG Auto Deploy New Setup",
         "Text-Verified Mortgage Protection"),
        ("OTP FEX", "Text Verified FEX Auto Deploy Cap Launch Date",
         "Text-Verified OTP FEX"),
        ("LP IUL", "Text Verified IUL Auto Deploy New Setup",
         "Text-Verified LP IUL"),
        # Already says Standard; saying it twice helps nobody.
        ("OTP Standard IUL", "Text Verified Standard IUL",
         "Text-Verified OTP Standard IUL"),
        ("Standard Mortgage Protection", "Mortgage Protection New Auto Deploy",
         "Standard Mortgage Protection"),
        # "No OTP" beats "OTP" - they are opposites.
        ("LP VET", "Landing Page VET No OTP Auto Deploy Row Weighted",
         "No OTP LP VET"),
        ("No OTP Standard VET", "VET Standard Auto Deploy", "No OTP Standard VET"),
        ("Spanish MTG", "Spanish MTG Auto Deploy New Setup", "Spanish MTG"),
        # One qualifier, not a stack: Text-Verified wins over Facebook here.
        ("OTP Spanish IUL", "Text Verified Spanish Facebook IUL Auto Deploy New",
         "Text-Verified OTP Spanish IUL"),
        ("Abandoned MTG", "Abandoned MTG Auto Deploy", "Abandoned MTG"),
        ("Instant IUL", "Instant IUL Auto Deploy New Setup", "Instant IUL"),
    ],
)
def test_the_qualifier_comes_off_the_deploy_sheet(lead_type, deploy, wanted):
    assert leadsheets.qualified_name(lead_type, deploy) == wanted


def test_the_full_title_carries_the_qualifier_and_the_year():
    assert leadsheets.new_sheet_title(
        leadsheets.qualified_name("Mortgage Protection", "Text Verified MTG Auto Deploy"),
        year=2026,
    ) == "Text-Verified Mortgage Protection Masterlist 2026"


def test_a_lead_type_with_no_deploy_sheet_keeps_its_own_name():
    assert leadsheets.qualified_name("Nova", "") == "Nova"


def test_the_qualified_name_still_matches_its_channel_next_run():
    """It has to be seen as that channel's masterlist, not a new file."""
    files = [{"id": "x", "name": "Text-Verified Mortgage Protection Masterlist 2026",
              "url": "u"}]
    assert leadsheets.find_sheet("Mortgage Protection", files) == files[0]


def test_only_one_qualifier_ever_lands_on_a_name():
    """"Text-Verified Facebook OTP Spanish IUL Masterlist 2026" is a file name
    nobody wants to read."""
    said = leadsheets.qualified_name(
        "Spanish IUL", "Text Verified Spanish Facebook Blue Collar Standard Auto Deploy"
    )
    assert said == "Text-Verified Spanish IUL"


# --------------------------------- never a second masterlist beside the first


def test_the_folder_is_matched_again_once_the_deploy_link_is_known():
    """`combine` runs before a sheet has been opened, so a channel linking a
    deploy sheet looks like it has a masterlist and its real one is passed
    over. That is how a second Mortgage Protection Masterlist got made."""
    held = Masterlist(category="MTG Masterlist", name="Mortgage Protection")
    files = [{"id": "m", "name": "Mortgage Protection Masterlist 2026", "url": "made"}]

    assert leadsheets.refill([held], files) == 1
    assert held.sheet == "made"
    assert leadsheets.missing([held]) == []


def test_refill_never_hands_a_lead_type_a_deploy_sheet():
    held = Masterlist(category="MTG Masterlist", name="Mortgage Protection")
    files = [{"id": "d", "name": "Mortgage Protection New Auto Deploy", "url": "u"}]

    assert leadsheets.refill([held], files) == 0
    assert held.sheet == ""


def test_a_qualified_name_already_in_the_folder_counts_as_the_same_lead_type():
    """"Text-Verified Mortgage Protection Masterlist 2026" is that channel's."""
    held = Masterlist(category="MTG Masterlist", name="Mortgage Protection")
    files = [{"id": "m", "name": "Text-Verified Mortgage Protection Masterlist 2026",
              "url": "made"}]

    assert leadsheets.refill([held], files) == 1
    assert held.sheet == "made"


def test_refill_leaves_alone_what_already_has_a_sheet():
    held = Masterlist(category="MTG", name="Mortgage Protection", sheet="pinned")
    files = [{"id": "m", "name": "Mortgage Protection Masterlist 2026", "url": "other"}]

    assert leadsheets.refill([held], files) == 0
    assert held.sheet == "pinned"
