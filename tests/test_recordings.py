"""Filing a posted sales-call recording into the Notion gallery.

The message that arrives looks like this, passcode on its own line:

    https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtm...
    Passcode: U^M^s7Bw
"""

from datetime import date

import pytest

from wilbyte import recordings

ZOOM = (
    "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"
    "RWEnkaraH75-X_WNCVYqV8cl.VeLdfj6Y0jsbRemx"
)


# ------------------------------------------------------------------ the link


@pytest.mark.parametrize(
    "text,platform",
    [
        (ZOOM, "Zoom"),
        ("https://fathom.video/share/abc123xyz", "Fathom"),
        ("https://www.youtube.com/watch?v=abc123", "YouTube"),
        ("https://youtu.be/abc123", "YouTube"),
    ],
)
def test_each_platform_is_recognised(text, platform):
    found = recordings.find_recording(text)

    assert found is not None
    assert found.platform == platform


def test_the_whole_zoom_url_survives():
    """These are enormous and full of dots and underscores - a clipped one is dead."""
    found = recordings.find_recording(f"{ZOOM}\nPasscode: U^M^s7Bw")

    assert found.url == ZOOM


def test_a_trailing_full_stop_is_not_part_of_the_link():
    found = recordings.find_recording("here it is https://youtu.be/abc123.")

    assert found.url == "https://youtu.be/abc123"


def test_a_message_with_no_recording_is_left_alone():
    assert recordings.find_recording("great call today, notes to follow") is None


def test_an_unrelated_link_is_not_a_recording():
    assert recordings.find_recording("see https://agentleadlab.com/blog") is None


# --------------------------------------------------------------- the passcode


def test_the_passcode_is_taken_from_the_line_below():
    found = recordings.find_recording(f"{ZOOM}\nPasscode: U^M^s7Bw")

    assert found.passcode == "U^M^s7Bw"


@pytest.mark.parametrize(
    "line",
    [
        "Passcode: U^M^s7Bw",
        "passcode U^M^s7Bw",
        "Password: U^M^s7Bw",
        "PASSCODE - U^M^s7Bw",
        "  Passcode:   U^M^s7Bw  ",
    ],
)
def test_however_the_passcode_line_is_typed(line):
    assert recordings.find_passcode(f"{ZOOM}\n{line}") == "U^M^s7Bw"


def test_punctuation_in_a_passcode_is_never_tidied():
    """`U^M^s7Bw` cleaned up is a passcode that looks right and doesn't work."""
    assert recordings.find_passcode("Passcode: a^b$c&d!e#f") == "a^b$c&d!e#f"


def test_a_passcode_with_spaces_survives_whole():
    assert recordings.find_passcode("Passcode: two words here") == "two words here"


def test_no_passcode_line_means_no_passcode():
    assert recordings.find_recording(ZOOM).passcode == ""


def test_the_word_passcode_mid_sentence_is_not_a_passcode():
    """"I'll send the passcode over later" is prose, not a credential.

    The label has to open the line, or half a sentence ends up in the field.
    """
    assert recordings.find_passcode(f"{ZOOM}\nI'll send the passcode over later") == ""


# ------------------------------------------------------------- the numbering


def test_numbering_starts_at_one_on_an_empty_gallery():
    assert recordings.next_number([]) == 1


def test_the_next_number_follows_the_highest_used():
    titles = ["Sales Recording 1", "Sales Recording 2", "Sales Recording 3"]

    assert recordings.next_number(titles) == 4


def test_a_deleted_row_does_not_free_up_its_number():
    """Reusing a number breaks every reference to the old one."""
    titles = ["Sales Recording 1", "Sales Recording 7"]

    assert recordings.next_number(titles) == 8


def test_rows_named_anything_else_are_ignored():
    titles = ["Kickoff call notes", "Sales Recording 4", "Sales Recording (old)"]

    assert recordings.next_number(titles) == 5


def test_a_parenthesised_number_still_counts():
    assert recordings.next_number(["Sales Recording (9)"]) == 10


def test_titles_are_read_out_of_notion_rows():
    rows = [
        {"properties": {"Name": {"type": "title", "title": [{"plain_text": "Sales Recording 3"}]}}},
        {"properties": {"Link": {"type": "url", "url": "https://x"}}},
    ]

    assert recordings.row_titles(rows) == ["Sales Recording 3"]


def test_the_title_reads_the_way_it_was_asked_for():
    assert recordings.title_for(12) == "Sales Recording 12"


# ------------------------------------------------------------ the notion row


def recording(**kwargs):
    base = {"url": ZOOM, "platform": "Zoom", "passcode": "U^M^s7Bw"}
    return recordings.Recording(**{**base, **kwargs})


def test_the_row_carries_the_link_and_passcode():
    props = recordings.page_properties(recording(), "Sales Recording 12")

    assert props["Name"]["title"][0]["text"]["content"] == "Sales Recording 12"
    assert props["Link"]["url"] == ZOOM
    assert props["Passcode"]["rich_text"][0]["text"]["content"] == "U^M^s7Bw"


def test_a_missing_passcode_is_left_blank_not_filled_in():
    props = recordings.page_properties(recording(passcode=""), "Sales Recording 1")

    assert "Passcode" not in props


def test_the_date_is_recorded_when_known():
    props = recordings.page_properties(
        recording(posted_on=date(2026, 8, 19)), "Sales Recording 1"
    )

    assert props["Date"]["date"]["start"] == "2026-08-19"


# --------------------------------------------------------- inside the card


def test_the_card_opens_with_the_recording_and_its_details():
    blocks = recordings.page_blocks(recording(posted_by="Santiago Villegas"))

    assert blocks[0]["bookmark"]["url"] == ZOOM
    detail = blocks[1]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "Zoom" in detail
    assert "U^M^s7Bw" in detail, "the line someone copies with the recording open"
    assert "Santiago Villegas" in detail


def test_a_summary_becomes_readable_blocks():
    blocks = recordings.page_blocks(
        recording(), summary="They pushed back on price.\n- Asked about aged leads\n- Wants a demo"
    )
    kinds = [block["type"] for block in blocks]

    assert "heading_2" in kinds
    assert kinds.count("bulleted_list_item") == 2


def test_no_summary_means_no_empty_heading():
    kinds = [block["type"] for block in recordings.page_blocks(recording(), summary="   ")]

    assert "heading_2" not in kinds


def test_youtube_can_always_be_read():
    assert recordings.Recording(url="x", platform="YouTube").transcribable(None) is True


def test_zoom_can_be_read_once_the_api_app_exists():
    """The share link is a browser door with a passcode on it; the API isn't."""
    from types import SimpleNamespace

    configured = SimpleNamespace(
        secrets=SimpleNamespace(
            zoom_account_id="a", zoom_client_id="b", zoom_client_secret="c"
        )
    )
    rec = recordings.Recording(url=ZOOM, platform="Zoom")

    assert rec.transcribable(configured) is True


def test_zoom_without_credentials_is_filed_without_a_summary():
    """Better a card with the link than a card with an invented summary."""
    from types import SimpleNamespace

    bare = SimpleNamespace(
        secrets=SimpleNamespace(zoom_account_id=None, zoom_client_id=None, zoom_client_secret=None)
    )

    assert recordings.Recording(url=ZOOM, platform="Zoom").transcribable(bare) is False


def test_fathom_is_not_wired_up_yet():
    assert recordings.Recording(url="x", platform="Fathom").transcribable(None) is False


# ------------------------------------------------------------- the schema


def test_the_gallery_has_the_columns_that_were_asked_for():
    schema = recordings.database_schema()

    assert set(schema) == {"Name", "Link", "Passcode", "Date"}
    assert schema["Name"] == {"title": {}}
    assert schema["Link"] == {"url": {}}


# ------------------------------------------------------ naming the card


def call(**kwargs):
    return recordings.Recording(**{"url": ZOOM, "platform": "Fathom", **kwargs})


def test_the_number_leads_and_the_names_follow():
    """The asked-for shape: Sales Recording (number) - (closer) (client)."""
    rec = call(closer="Santiago Villegas", guests=("Derrick Robison",))

    assert recordings.call_title(rec, 3) == (
        "Sales Recording 3 - Santiago Villegas Derrick Robison"
    )


def test_the_first_guest_is_the_client():
    rec = call(closer="Santiago", guests=("Derrick Robison", "Someone Else"))

    assert recordings.call_title(rec, 4) == "Sales Recording 4 - Santiago Derrick Robison"


def test_a_closer_with_no_client_still_names_the_card():
    assert recordings.call_title(call(closer="Santiago"), 4) == "Sales Recording 4 - Santiago"


def test_the_meeting_title_is_the_next_best_thing():
    assert recordings.call_title(call(topic="Discovery Call"), 5) == (
        "Sales Recording 5 - Discovery Call"
    )


def test_a_call_with_no_names_at_all_is_still_the_number():
    """A card called "Sales Recording 6" is findable; one ending in " - " is not."""
    assert recordings.call_title(call(), 6) == "Sales Recording 6"


def test_blank_guest_names_are_not_mistaken_for_a_client():
    rec = call(closer="Santiago", guests=("", "   ", "Derrick Robison"))

    assert recordings.call_title(rec, 7) == "Sales Recording 7 - Santiago Derrick Robison"


# ------------------------------------- saying why there is no summary

# A card filed with no summary and no explanation reads as "nothing was said on
# that call". These are the two ordinary reasons, and both have to reach Discord.


class _FakeZoom:
    """Stands in for ZoomClient. `meetings` is what the account holds."""

    meetings: list = []
    text = ""

    def __init__(self, *args, **kwargs):
        pass

    def account_recordings(self, **kwargs):
        return type(self).meetings

    def transcript(self, recording):
        return type(self).text

    def close(self):
        pass


def _zoom_config():
    from types import SimpleNamespace

    return SimpleNamespace(
        secrets=SimpleNamespace(
            zoom_account_id="a", zoom_client_id="b", zoom_client_secret="c"
        )
    )


def _use_fake(monkeypatch, meetings, text=""):
    from wilbyte import zoom

    _FakeZoom.meetings = meetings
    _FakeZoom.text = text
    monkeypatch.setattr(zoom, "ZoomClient", _FakeZoom)


def test_a_zoom_call_that_cannot_be_found_says_so(monkeypatch):
    from wilbyte.bot import jobs

    _use_fake(monkeypatch, [{"topic": "Someone else", "share_url": "https://z/rec/share/xx.y"}])
    rec = call(platform="Zoom")

    assert jobs.zoom_transcript(_zoom_config(), rec) == ""
    assert "didn't match" in rec.note.casefold()
    # How many were looked at: none is a setup problem, 92 is a matching one.
    assert "1 zoom recording" in rec.note.casefold()


def test_a_zoom_call_recorded_without_transcription_says_so(monkeypatch):
    """The trap: everything configured right, and the setting was off that day."""
    from wilbyte.bot import jobs

    _use_fake(monkeypatch, [{"topic": "Discovery Call", "share_url": ZOOM}])
    rec = call(platform="Zoom")

    assert jobs.zoom_transcript(_zoom_config(), rec) == ""
    assert "transcript" in rec.note.casefold()
    # Found, so the meeting details still come back for the title.
    assert rec.topic == "Discovery Call"


def test_a_call_that_worked_carries_no_complaint(monkeypatch):
    from wilbyte.bot import jobs

    _use_fake(
        monkeypatch,
        [{
            "topic": "Discovery Call",
            "share_url": ZOOM,
            "host_email": "santi@agentleadlab.com",
            "recording_files": [
                {"file_type": "TRANSCRIPT", "download_url": "https://x/t.vtt"}
            ],
        }],
        text="Santiago Villegas: hello there Derrick Robison: hey man ",
    )
    rec = call(platform="Zoom")

    jobs.zoom_transcript(_zoom_config(), rec)

    assert rec.note == ""


def test_the_names_for_the_card_come_out_of_the_transcript(monkeypatch):
    """Zoom's API gives a host email and a topic; the people are in the words."""
    from wilbyte.bot import jobs

    _use_fake(
        monkeypatch,
        [{
            "topic": "Derrick Robison",
            "share_url": ZOOM,
            "host_email": "santi@agentleadlab.com",
            "recording_files": [
                {"file_type": "TRANSCRIPT", "download_url": "https://x/t.vtt"}
            ],
        }],
        text="Santiago Villegas: morning Derrick Robison: hey man ",
    )
    rec = call(platform="Zoom")

    jobs.zoom_transcript(_zoom_config(), rec)

    assert rec.closer == "Santiago Villegas"
    assert rec.guests == ("Derrick Robison",)
    assert recordings.call_title(rec, 2) == (
        "Sales Recording 2 - Santiago Villegas Derrick Robison"
    )
