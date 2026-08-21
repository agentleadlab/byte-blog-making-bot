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


def test_the_people_are_the_title():
    """The asked-for shape: Sales Recording: (closer) (client)."""
    rec = call(closer="Santiago Villegas", guests=("Derrick Robison",))

    assert recordings.call_title(rec) == (
        "Sales Recording: Santiago Villegas + Derrick Robison"
    )


def test_the_first_guest_is_the_client():
    rec = call(closer="Santiago", guests=("Derrick Robison", "Someone Else"))

    assert recordings.call_title(rec) == "Sales Recording: Santiago + Derrick Robison"


def test_a_closer_with_no_client_still_names_the_card():
    assert recordings.call_title(call(closer="Santiago")) == "Sales Recording: Santiago"


def test_the_meeting_title_is_the_next_best_thing():
    assert recordings.call_title(call(topic="Discovery Call")) == (
        "Sales Recording: Discovery Call"
    )


def test_a_call_with_no_names_at_all_still_reads_cleanly():
    """A title trailing off after the colon looks broken. This just stops."""
    assert recordings.call_title(call()) == "Sales Recording"


def test_blank_guest_names_are_not_mistaken_for_a_client():
    rec = call(closer="Santiago", guests=("", "   ", "Derrick Robison"))

    assert recordings.call_title(rec) == "Sales Recording: Santiago + Derrick Robison"


# ------------------------------------- saying why there is no summary

# A card filed with no summary and no explanation reads as "nothing was said on
# that call". These are the two ordinary reasons, and both have to reach Discord.


class _FakeZoom:
    """Stands in for ZoomClient. `meetings` is what the account holds."""

    meetings: list = []
    text = ""
    page_topic = ""

    def __init__(self, *args, **kwargs):
        pass

    def account_recordings(self, **kwargs):
        return type(self).meetings

    def share_page_topic(self, url):
        return type(self).page_topic

    def transcript_vtt(self, recording):
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


# Zoom writes one speaker per cue line. Flattened prose was what broke the
# names in the first place, so the fake serves the real shape.
_VTT = """WEBVTT

1
00:00:08.000 --> 00:00:10.000
Santiago Villegas: Derrick, what's going on, brother? Good morning.

2
00:00:10.500 --> 00:00:12.000
Derrick Robison: Hey man, good morning.
"""


def _use_fake(monkeypatch, meetings, text="", page_topic=""):
    from wilbyte import zoom

    _FakeZoom.meetings = meetings
    _FakeZoom.text = text
    _FakeZoom.page_topic = page_topic
    monkeypatch.setattr(zoom, "ZoomClient", _FakeZoom)


def test_a_zoom_call_that_cannot_be_found_says_so(monkeypatch):
    from wilbyte.bot import jobs

    _use_fake(monkeypatch, [{"topic": "Someone else", "share_url": "https://z/rec/share/xx.y"}])
    rec = call(platform="Zoom")

    assert jobs.zoom_transcript(_zoom_config(), rec) == ""
    assert "can't tell which call" in rec.note.casefold()
    # And what to do about it, rather than just that it happened.
    assert "passcode" in rec.note.casefold()


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
        text=_VTT,
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
        text=_VTT,
    )
    rec = call(platform="Zoom")

    jobs.zoom_transcript(_zoom_config(), rec)

    assert rec.closer == "Santiago Villegas"
    assert rec.guests == ("Derrick Robison",)
    assert recordings.call_title(rec) == (
        "Sales Recording: Santiago Villegas + Derrick Robison"
    )


# --------------------------------------------- picking the call by hand


def test_a_picked_call_is_read_and_named(monkeypatch):
    """The fallback when a link can't be matched: read the one that was chosen."""
    from wilbyte import zoom
    from wilbyte.bot import jobs

    _use_fake(monkeypatch, [], text=_VTT)
    picked = zoom.ZoomRecording(
        topic="Derrick Robison",
        share_url=ZOOM,
        host_email="santi@agentleadlab.com",
        transcript_url="https://x/t.vtt",
    )
    rec = call(platform="Zoom")

    text = jobs.zoom_read(_zoom_config(), rec, picked)

    assert "Derrick Robison" in text
    assert rec.closer == "Santiago Villegas"
    assert rec.guests == ("Derrick Robison",)
    assert rec.topic == "Derrick Robison"


def test_a_sentence_can_never_become_the_title():
    """This shipped: "Sales Recording 1 - Santiago Villegas Agent Lead Lab How
    are you? Good morning. Derrick Robison". The reader is fixed upstream; this
    is the guard that stops the next variant reaching a card."""
    rec = call(closer="Santiago Villegas Agent Lead Lab How are you? Good morning.")

    assert recordings.call_title(rec) == "Sales Recording"


def test_a_guest_that_is_a_sentence_is_skipped_for_one_that_is_not():
    rec = call(closer="Santiago", guests=("Well, here is the thing. Derrick", "Derrick Robison"))

    assert recordings.call_title(rec) == "Sales Recording: Santiago + Derrick Robison"


# ------------------------------------- remembering what has been filed

def test_a_filed_call_is_remembered(tmp_path):
    store = tmp_path / "filed.json"

    assert recordings.filed_ids(store) == set()

    recordings.remember_filed("abc==", store)
    recordings.remember_filed("def==", store)

    assert recordings.filed_ids(store) == {"abc==", "def=="}


def test_remembering_the_same_call_twice_is_harmless(tmp_path):
    store = tmp_path / "filed.json"
    recordings.remember_filed("abc==", store)
    recordings.remember_filed("abc==", store)

    assert recordings.filed_ids(store) == {"abc=="}


def test_a_corrupt_store_is_treated_as_empty(tmp_path):
    """Better to re-offer a call than to refuse to file anything."""
    store = tmp_path / "filed.json"
    store.write_text("{not json", encoding="utf-8")

    assert recordings.filed_ids(store) == set()


# ------------------------------------------ picking a call by typing its name


def a_call(**kwargs):
    from wilbyte.bot import jobs

    base = {
        "platform": "zoom", "uid": "abc==", "topic": "Derrick Robison",
        "when": "2026-08-19T05:17:00Z", "who": "santi@agentleadlab.com",
    }
    return jobs.Call(**{**base, **kwargs})


def test_typing_part_of_a_name_finds_the_call():
    assert a_call().matches("derr")
    assert a_call().matches("Derrick Robison")


def test_the_search_ignores_case_and_word_order():
    assert a_call().matches("robison derrick")


def test_a_date_narrows_it_down():
    assert a_call().matches("derrick 2026-08-19")
    assert not a_call().matches("derrick 2026-08-21")


def test_the_closer_can_be_searched_for_too():
    assert a_call().matches("santi")


def test_an_empty_box_offers_everything():
    assert a_call().matches("")


def test_a_name_that_is_not_there_matches_nothing():
    assert not a_call().matches("arlene")


def test_the_label_and_value_fit_what_discord_allows():
    """Discord truncates past 100 characters, which would break the lookup."""
    long_call = a_call(topic="x" * 300, uid="y" * 300)

    assert len(long_call.label) <= 100
    assert len(long_call.key) <= 100


def test_the_platform_is_part_of_the_key():
    """Zoom and Fathom ids are unrelated and could collide."""
    assert a_call().key.startswith("zoom|")


# ------------------------------------- the name typed into the message

# "Sales: Derrick Robison <link>" is how these get posted anyway, and it earns
# its keep twice: it names the card without anything being read, and because
# Zoom titles a recording after whoever was on it, it is also the search that
# finds the call - which is what makes the common case need no dropdown.

LINK = "https://us06web.zoom.us/rec/share/abc.def"


@pytest.mark.parametrize(
    "line",
    [
        "Sales: Derrick Robison",
        "sales - Derrick Robison",
        "Sale: Derrick Robison",
        "Client: Derrick Robison",
        "call: Derrick Robison",
        "with: Derrick Robison",
    ],
)
def test_however_the_client_line_is_typed(line):
    assert recordings.find_client(f"{line} {LINK}") == "Derrick Robison"


def test_the_passcode_is_not_read_as_a_client():
    assert recordings.find_client(f"{LINK}\nPasscode: U^M^s7Bw") == ""


def test_a_link_on_its_own_names_nobody():
    assert recordings.find_client(LINK) == ""


def test_a_sentence_is_not_a_client_name():
    """"call: I'll send it over later, thanks" is prose that happens to fit."""
    assert recordings.find_client("call: I'll send this one over later, thanks all") == ""


def test_the_url_is_never_part_of_the_name():
    assert LINK not in recordings.find_client(f"Sales: Derrick Robison {LINK}")


def test_the_typed_name_titles_the_card_with_nothing_read():
    """A call that can't be read still files under the right heading."""
    rec = recordings.find_recording(f"Sales: Derrick Robison {LINK}")
    rec.posted_by = "Santiago Villegas"

    assert recordings.call_title(rec) == (
        "Sales Recording: Santiago Villegas + Derrick Robison"
    )


def test_names_read_off_the_call_beat_the_typed_ones():
    """The transcript knows who was actually there; the message is a stand-in."""
    rec = recordings.find_recording(f"Sales: Derrick R {LINK}")
    rec.posted_by = "Franklin"
    rec.closer, rec.guests = "Santiago Villegas", ("Derrick Robison",)

    assert recordings.call_title(rec) == (
        "Sales Recording: Santiago Villegas + Derrick Robison"
    )


def test_the_poster_stands_in_for_the_closer():
    """Whoever posts a recording is nearly always the closer who was on it."""
    rec = recordings.find_recording(LINK)
    rec.posted_by = "Santiago Villegas"

    assert recordings.call_title(rec) == "Sales Recording: Santiago Villegas"


# ------------------------------------- getting a card back out again


def notion_row(title, url="https://notion.so/x", link=""):
    return {
        "url": url,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "Link": {"type": "url", "url": link},
        },
    }


def test_the_asking_is_stripped_out_of_the_question():
    assert recordings.wanted_name(
        "<@1> need the sales recording for Derrick Robison call"
    ) == "Derrick Robison"


def test_a_link_in_the_question_is_not_part_of_the_name():
    assert recordings.wanted_name("find the recording https://notion.so/abc") == ""


def test_the_card_is_found_by_the_client_name():
    rows = [
        notion_row("Sales Recording: Santiago Villegas + Derrick Robison", "https://n/1"),
        notion_row("Sales Recording: Santiago Villegas + Arlene Linares", "https://n/2"),
    ]

    assert recordings.matching_rows(rows, "Derrick Robison") == [
        ("Sales Recording: Santiago Villegas + Derrick Robison", "https://n/1", "")
    ]


def test_the_closer_name_finds_all_of_their_calls():
    rows = [
        notion_row("Sales Recording: Santiago Villegas + Derrick Robison"),
        notion_row("Sales Recording: Tre Tarpley + Brice Barker"),
    ]

    assert len(recordings.matching_rows(rows, "Santiago")) == 1


def test_every_word_has_to_match_not_just_one():
    """"derrick robison" must not also return every other Derrick."""
    rows = [
        notion_row("Sales Recording: Santi + Derrick Robison"),
        notion_row("Sales Recording: Santi + Derrick Someone-Else"),
    ]

    assert len(recordings.matching_rows(rows, "derrick robison")) == 1


def test_asking_for_nothing_in_particular_returns_everything():
    """"send me the sales recordings" is a reasonable thing to say."""
    rows = [notion_row("Sales Recording: A + B"), notion_row("Sales Recording: C + D")]

    assert len(recordings.matching_rows(rows, "")) == 2


def test_a_name_nobody_has_matches_nothing():
    assert recordings.matching_rows([notion_row("Sales Recording: A + B")], "Nobody") == []


def test_a_row_with_no_title_is_skipped():
    assert recordings.matching_rows([{"url": "https://n/1", "properties": {}}], "") == []


def test_the_recording_link_comes_back_with_the_card():
    """"Need the video of Derrick" wants the video, not a page to click through."""
    rows = [notion_row("Sales Recording: Santi + Derrick", link="https://zoom.us/rec/share/x")]

    assert recordings.matching_rows(rows, "derrick")[0][2] == "https://zoom.us/rec/share/x"


def test_a_card_with_no_link_still_comes_back():
    rows = [notion_row("Sales Recording: Santi + Derrick")]

    title, card, link = recordings.matching_rows(rows, "derrick")[0]
    assert card and not link


# ---------------------------------------- the summary as a readable page

# The model writes markdown and Notion has never heard of it, so "**What the
# prospect wanted**" arrived on the card as literal asterisks.


def kinds_and_text(blocks):
    out = []
    for block in blocks:
        kind = block["type"]
        text = "".join(run["text"]["content"] for run in block[kind]["rich_text"])
        out.append((kind, text))
    return out


def test_bold_inside_a_line_becomes_a_mark_not_asterisks():
    blocks = recordings.summary_blocks("**Overview:** They bought 25 leads.")
    runs = blocks[0]["paragraph"]["rich_text"]

    assert runs[0]["text"]["content"] == "Overview:"
    assert runs[0]["annotations"] == {"bold": True}
    assert "*" not in "".join(run["text"]["content"] for run in runs)


def test_a_line_that_is_only_a_bold_phrase_is_a_heading():
    """It was being used as a section title, so make it one."""
    assert kinds_and_text(recordings.summary_blocks("**What the prospect wanted**")) == [
        ("heading_3", "What the prospect wanted")
    ]


def test_a_trailing_colon_on_a_section_title_comes_off():
    assert kinds_and_text(recordings.summary_blocks("**Objections raised:**")) == [
        ("heading_3", "Objections raised")
    ]


def test_markdown_headings_are_headings_too():
    blocks = kinds_and_text(recordings.summary_blocks("## Summary\n### Detail"))

    assert blocks == [("heading_2", "Summary"), ("heading_3", "Detail")]


def test_bullets_and_numbers_each_get_their_own_kind():
    blocks = recordings.summary_blocks("- first\n1. second\n2) third")

    assert [k for k, _ in kinds_and_text(blocks)] == [
        "bulleted_list_item", "numbered_list_item", "numbered_list_item"
    ]


def test_the_numbering_marker_is_not_repeated_in_the_text():
    """Notion numbers the item itself; leaving "1." in prints "1. 1. ...""."""
    assert kinds_and_text(recordings.summary_blocks("1. Trial batch of 25 leads")) == [
        ("numbered_list_item", "Trial batch of 25 leads")
    ]


def test_italics_survive_as_italics():
    runs = recordings.summary_blocks("He was *very* clear.")[0]["paragraph"]["rich_text"]

    assert any(run.get("annotations") == {"italic": True} for run in runs)


def test_a_lone_asterisk_is_left_alone():
    """"$35/lead * 25" is arithmetic, not emphasis."""
    text = "".join(
        run["text"]["content"]
        for run in recordings.summary_blocks("$35/lead * 25")[0]["paragraph"]["rich_text"]
    )

    assert text == "$35/lead * 25"


def test_blank_lines_do_not_become_empty_blocks():
    assert len(recordings.summary_blocks("one\n\n\ntwo")) == 2


def test_a_passcode_with_asterisks_survives_the_card_body():
    """`a*b*c` read as emphasis becomes `abc` - right-looking and useless."""
    blocks = recordings.page_blocks(recording(passcode="a*b*c"))
    detail = "".join(
        run["text"]["content"] for run in blocks[1]["paragraph"]["rich_text"]
    )

    assert "a*b*c" in detail


# ------------------------------------ a Discord name is not a person's name

# Whoever posts a recording stands in for the closer, so their display name
# ends up on a card - and Discord names carry job titles, teams and emoji.
# "Sales Recording: Franklin 🐻| General Manager" is what that looked like.


@pytest.mark.parametrize(
    "display,expected",
    [
        ("Franklin 🐻| General Manager", "Franklin"),
        ("tre | Closer", "tre"),
        ("Luna · the whole thing", "Luna"),
        ("Tre - Closer", "Tre"),
        ("Kiki ✨", "Kiki"),
        ("Santiago Villegas", "Santiago Villegas"),
    ],
)
def test_the_decoration_comes_off_a_display_name(display, expected):
    assert recordings.person_name(display) == expected


@pytest.mark.parametrize("name", ["Mary-Jane O'Brien", "Jean-Luc", "Ann-Marie"])
def test_a_hyphen_inside_a_name_survives(name):
    """A dash only separates when it stands alone with spaces around it."""
    assert recordings.person_name(name) == name


def test_an_empty_display_name_is_not_a_name():
    assert recordings.person_name("") == ""
    assert recordings.person_name("🐻") == ""


def test_the_card_title_uses_the_cleaned_poster_name():
    rec = call(client_hint="Derrick Robison")
    rec.posted_by = "Franklin 🐻| General Manager"

    assert recordings.call_title(rec) == "Sales Recording: Franklin + Derrick Robison"


# ------------------------------- an empty summary always explains itself

# Three cards have been filed with no summary and no reason given, and each
# time the cause turned out to be somewhere different. Every way this can come
# back empty now says which one it was.


def test_an_empty_transcript_says_so_rather_than_returning_nothing():
    from types import SimpleNamespace

    from wilbyte.bot import jobs
    from wilbyte.copywriter import CopywriterError

    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k"))

    with pytest.raises(CopywriterError) as caught:
        jobs.summarise_text(config, "   ")

    assert "nothing to summarise" in str(caught.value)


def test_a_refusal_from_claude_becomes_a_readable_reason(monkeypatch):
    from types import SimpleNamespace

    from wilbyte.bot import jobs
    from wilbyte.copywriter import CopywriterError

    class Boom:
        def __init__(self, **kwargs):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("overloaded_error: server is busy")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    config = SimpleNamespace(
        secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
        copy=SimpleNamespace(model="claude"),
    )

    with pytest.raises(CopywriterError) as caught:
        jobs.summarise_text(config, "Santiago: hello. Derrick: hi.")

    assert "overloaded" in str(caught.value)


def test_claude_returning_nothing_is_reported_with_its_stop_reason(monkeypatch):
    from types import SimpleNamespace

    from wilbyte.bot import jobs
    from wilbyte.copywriter import CopywriterError

    class Empty:
        def __init__(self, **kwargs):
            self.messages = self

        def create(self, **kwargs):
            return SimpleNamespace(content=[], stop_reason="max_tokens")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", Empty)
    config = SimpleNamespace(
        secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
        copy=SimpleNamespace(model="claude"),
    )

    with pytest.raises(CopywriterError) as caught:
        jobs.summarise_text(config, "Santiago: hello. Derrick: hi.")

    assert "max_tokens" in str(caught.value)


# ------------------------------ filing calls without being asked

# Posting a link and then being told which call it was is two steps more than
# nobody doing anything. Zoom and Fathom both know what was recorded and when.


def a_zoom_call(topic, when, *, transcript=True):
    from wilbyte.bot import jobs

    files = [{"file_type": "TRANSCRIPT", "download_url": "https://x/t.vtt"}] if transcript else []
    return jobs.Call(
        "zoom", f"uid-{topic}", topic, when, "santi@agentleadlab.com",
        {"topic": topic, "start_time": when, "recording_files": files},
    )


@pytest.mark.parametrize(
    "topic",
    ["Daily Team Call: 🙏", "EOD Team Call", "Weekly sync", "1:1 with Tre", "Interview — Ops"],
)
def test_recurring_internal_calls_are_not_sales_calls(topic):
    """These are recorded daily and would bury the gallery in standups."""
    from wilbyte.bot import jobs

    assert jobs.is_internal(topic) is True


@pytest.mark.parametrize(
    "topic",
    ["Derrick Robison", "Strategy Session — Brice Barker", "Fran – Compra de Leads"],
)
def test_a_client_call_is_never_treated_as_internal(topic):
    from wilbyte.bot import jobs

    assert jobs.is_internal(topic) is False


def test_only_calls_with_something_to_read_are_swept(monkeypatch):
    from wilbyte.bot import jobs

    assert jobs._has_text(a_zoom_call("Derrick", "2026-08-21T10:00:00Z")) is True
    assert jobs._has_text(a_zoom_call("Derrick", "2026-08-21T10:00:00Z", transcript=False)) is False


def test_a_fathom_call_always_has_something_to_read():
    """Fathom writes its own summary, so there is always a card worth making."""
    from wilbyte.bot import jobs

    assert jobs._has_text(jobs.Call("fathom", "f1", "Brice", "2026-08-21T10:00:00Z", "Tre", {}))


def test_the_sweep_skips_what_is_filed_internal_or_old(monkeypatch, tmp_path):
    from wilbyte.bot import jobs

    calls = [
        a_zoom_call("Derrick Robison", "2026-08-21T10:00:00Z"),
        a_zoom_call("Daily Team Call: 🙏", "2026-08-21T09:00:00Z"),
        a_zoom_call("Already Filed", "2026-08-21T08:00:00Z"),
        a_zoom_call("Ancient History", "2026-01-01T08:00:00Z"),
        a_zoom_call("No Transcript", "2026-08-21T07:00:00Z", transcript=False),
    ]
    monkeypatch.setattr(jobs, "call_choices", lambda config, force=False: calls)
    monkeypatch.setattr(
        recordings, "filed_ids", lambda path=None: {"uid-Already Filed"}
    )

    import datetime as real_datetime

    class Now(real_datetime.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 8, 21, 12, 0)

    monkeypatch.setattr(real_datetime, "datetime", Now)
    found = jobs.new_recordings(None)

    assert [call.topic for call in found] == ["Derrick Robison"]
