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
