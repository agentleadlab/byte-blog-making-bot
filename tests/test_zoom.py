"""Reading a Zoom sales call the API rather than through the share link.

The share link is a browser door with a passcode on it. The API is a different
door, authenticated as the account, and it hands over the transcript Zoom made
when the call was recorded - if audio transcript was on at the time.
"""

import pytest

from wilbyte import zoom

SHARE = "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtm.VeLdfj6Y0jsbRemx"


def meeting(share_url=SHARE, *, topic="Sales Call", files=None, **extra):
    return {
        "topic": topic,
        "share_url": share_url,
        "host_email": "santiago@agentleadlab.com",
        "start_time": "2026-08-19T18:00:00Z",
        "recording_files": files if files is not None else [],
        **extra,
    }


def transcript_file(url="https://zoom.us/rec/download/abc"):
    return {"file_type": "TRANSCRIPT", "download_url": url}


# ---------------------------------------------------- matching a pasted link


def test_a_pasted_link_finds_its_meeting():
    found = zoom.match_share_url([meeting()], SHARE)

    assert found is not None
    assert found.topic == "Sales Call"


def test_a_passcode_query_on_the_link_still_matches():
    """Zoom hands out the same recording with `?pwd=` appended."""
    assert zoom.match_share_url([meeting()], SHARE + "?pwd=U^M^s7Bw") is not None


def test_a_trailing_slash_still_matches():
    assert zoom.match_share_url([meeting()], SHARE + "/") is not None


def test_a_different_regional_host_still_matches():
    """us06web vs us05web is the same recording - the token is the identity."""
    other = SHARE.replace("us06web", "us05web")

    assert zoom.match_share_url([meeting(share_url=other)], SHARE) is not None


def test_a_different_recording_does_not_match():
    other = "https://us06web.zoom.us/rec/share/COMPLETELYDIFFERENTTOKEN.abc"

    assert zoom.match_share_url([meeting(share_url=other)], SHARE) is None


def test_no_meetings_is_not_an_error():
    assert zoom.match_share_url([], SHARE) is None


def test_an_empty_link_matches_nothing():
    """Otherwise a blank share_url in the API response would match a blank query."""
    assert zoom.match_share_url([meeting(share_url="")], "") is None


# ------------------------------------------------------- finding the transcript


def test_the_transcript_is_picked_out_of_the_recording_files():
    files = [
        {"file_type": "MP4", "download_url": "https://zoom.us/video"},
        {"file_type": "M4A", "download_url": "https://zoom.us/audio"},
        transcript_file(),
        {"file_type": "CHAT", "download_url": "https://zoom.us/chat"},
    ]

    assert zoom.pick_transcript(files) == "https://zoom.us/rec/download/abc"


def test_no_transcript_file_is_a_normal_answer():
    """Audio transcript was off when this was recorded. Not a failure."""
    files = [{"file_type": "MP4", "download_url": "https://zoom.us/video"}]

    assert zoom.pick_transcript(files) == ""


def test_a_recording_says_whether_it_can_be_read():
    with_text = zoom.as_recording(meeting(files=[transcript_file()]))
    without = zoom.as_recording(meeting())

    assert with_text.has_transcript
    assert not without.has_transcript


def test_the_meeting_details_are_carried_across():
    found = zoom.as_recording(meeting(topic="Ryan Egert - Agent Lead Lab"))

    assert found.topic == "Ryan Egert - Agent Lead Lab"
    assert found.host_email == "santiago@agentleadlab.com"
    assert found.started_at == "2026-08-19T18:00:00Z"


# -------------------------------------------------------------- share keys


@pytest.mark.parametrize(
    "url",
    [
        SHARE,
        SHARE + "/",
        SHARE + "?pwd=abc",
        SHARE + "#play",
        SHARE.upper().replace("HTTPS://US06WEB.ZOOM.US", "https://us06web.zoom.us"),
    ],
)
def test_every_dressing_of_a_link_reduces_to_the_same_key(url):
    assert zoom.share_key(url) == zoom.share_key(SHARE)


def test_a_link_that_is_not_a_share_link_keeps_its_shape():
    """A meeting join link isn't a recording, and must not collide with one."""
    assert zoom.share_key("https://us06web.zoom.us/j/83603938279") != zoom.share_key(SHARE)


@pytest.mark.parametrize("url", ["", "   ", None])
def test_junk_reduces_to_nothing(url):
    assert zoom.share_key(url) == ""


# ------------------------------------ the same recording, two different links

# A share token comes as `<token>.<suffix>`, and the suffix changes between the
# link the API reports and the one someone copies out of the browser. Comparing
# the whole string called those two different recordings - which is why a call
# that was plainly on the account, with a transcript, came back "not found".

REAL = (
    "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"
    "RWEnkaraH75-X_WNCVYqV8cl.VeLdfj6Y0jsbRemx"
)
SAME_CALL_OTHER_TAIL = (
    "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"
    "RWEnkaraH75-X_WNCVYqV8cl.aBcDeF9Z0jsbXxYz"
)


def test_a_link_matches_itself():
    assert zoom.same_recording(REAL, REAL) is True


def test_the_tail_after_the_dot_is_not_the_identity():
    assert zoom.same_recording(REAL, SAME_CALL_OTHER_TAIL) is True


def test_a_query_string_does_not_break_the_match():
    assert zoom.same_recording(REAL, f"{REAL}?pwd=abc123") is True


def test_two_different_recordings_still_do_not_match():
    other = "https://us06web.zoom.us/rec/share/zzzzzzzzzzzzzzzzzzzzzzzzzzzz.VeLdfj6Y"

    assert zoom.same_recording(REAL, other) is False


def test_a_short_token_is_not_matched_loosely():
    """A loose rule on a short token would match half the account."""
    assert zoom.same_recording(
        "https://us06web.zoom.us/rec/share/abc.one",
        "https://us06web.zoom.us/rec/share/abcdefghijklmnop.two",
    ) is False


def test_a_missing_link_matches_nothing():
    assert zoom.same_recording("", REAL) is False


def test_the_meeting_is_found_by_a_forwarded_link():
    meetings = [
        {"topic": "Someone Else", "share_url": "https://us06web.zoom.us/rec/share/other.x"},
        {"topic": "Derrick Robison", "share_url": REAL, "host_email": "santi@agentleadlab.com"},
    ]

    found = zoom.match_share_url(meetings, SAME_CALL_OTHER_TAIL)

    assert found is not None
    assert found.topic == "Derrick Robison"


def test_a_per_file_player_link_also_finds_the_meeting():
    """A link copied from the player belongs to the file, not the meeting."""
    meetings = [{
        "topic": "Derrick Robison",
        "share_url": "https://us06web.zoom.us/rec/share/meeting-level-token-here.aa",
        "recording_files": [{"file_type": "MP4", "play_url": REAL}],
    }]

    assert zoom.match_share_url(meetings, REAL).topic == "Derrick Robison"


def test_an_unmatched_link_is_explained_with_the_real_tokens():
    """"Not found" is useless; the strings that were compared are not."""
    meetings = [{"topic": "Derrick Robison", "share_url": REAL, "start_time": "2026-08-19T05:17:00Z"}]

    report = zoom.describe_match(meetings, "https://us06web.zoom.us/rec/share/nope.x")

    assert "nope" in report
    assert "Derrick Robison" in report
    assert "2026-08-19" in report


# --------------------------------------------- who was actually on the call

# A real Zoom transcript, in the shape Zoom actually writes it. The names live
# at the start of a cue line, and that is the only thing that makes them names.
TRANSCRIPT = """WEBVTT

1
00:00:08.000 --> 00:00:10.000
Santiago Villegas Agent Lead Lab: Derrick, what's going on, brother? How are you? Good morning.

2
00:00:10.500 --> 00:00:12.000
Derrick Robison: Hey man, good morning, how are you?

3
00:00:12.500 --> 00:00:14.000
Santiago Villegas Agent Lead Lab: Can you hear me okay?
"""


# The same call flattened into prose - what the speaker reader used to be given.
FLATTENED = (
    "Santiago Villegas Agent Lead Lab: Derrick, what's going on, brother? How "
    "are you? Good morning. Derrick Robison: Hey man, good morning, how are you?"
)


def test_a_sentence_is_never_read_as_a_name():
    """This exact string reached a real card: "... Good morning. Derrick Robison"."""
    for name in zoom.speakers(FLATTENED):
        assert "?" not in name and "." not in name


def test_the_company_suffix_comes_off_a_display_name():
    """Everyone on the team has it, on every card, forever."""
    assert zoom.strip_org(
        "Santiago Villegas Agent Lead Lab", "santi@agentleadlab.com"
    ) == "Santiago Villegas"


def test_a_name_without_the_company_is_left_alone():
    assert zoom.strip_org("Derrick Robison", "santi@agentleadlab.com") == "Derrick Robison"


def test_a_cue_that_is_a_sentence_with_a_colon_is_not_a_speaker():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nSo here's the thing: it worked.\n"

    assert zoom.speakers(vtt) == ()


def test_the_speakers_come_out_in_the_order_they_spoke():
    assert zoom.speakers(TRANSCRIPT) == (
        "Santiago Villegas Agent Lead Lab", "Derrick Robison"
    ), "speakers() reports the labels verbatim; the company comes off later"


def test_the_host_is_the_one_whose_name_matches_the_account():
    closer, guests = zoom.host_and_guests(TRANSCRIPT, "santi@agentleadlab.com")

    assert closer == "Santiago Villegas"
    assert guests == ("Derrick Robison",)


def test_an_unrecognised_host_leaves_everyone_a_guest():
    """Better no closer than the wrong one on the card."""
    closer, guests = zoom.host_and_guests(TRANSCRIPT, "nobody@example.com")

    assert closer == ""
    assert guests == ("Santiago Villegas Agent Lead Lab", "Derrick Robison")


def test_a_transcript_with_no_speaker_labels_names_nobody():
    assert zoom.host_and_guests("just some words with no labels", "santi@x.com") == ("", ())


# ------------------------------------------ the API and the browser disagree

# A browser share link is `/rec/share/<token>`; the API answers with
# `/recording/share/<token>`. Knowing only the first meant every link Zoom
# returned kept its whole URL as its "token" and matched nothing at all.

API_FORM = "https://zoom.us/recording/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"
BROWSER_FORM = "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"


def test_the_api_path_is_stripped_too():
    assert zoom.share_key(API_FORM) == zoom.share_key(BROWSER_FORM)


def test_the_two_forms_are_the_same_recording():
    assert zoom.same_recording(API_FORM, BROWSER_FORM) is True


def test_a_meeting_listed_in_the_api_form_is_found_from_a_browser_link():
    meetings = [{"topic": "Derrick Robison", "share_url": API_FORM}]

    assert zoom.match_share_url(meetings, BROWSER_FORM).topic == "Derrick Robison"


def test_a_play_link_reduces_to_the_same_token():
    play = "https://us06web.zoom.us/rec/play/qVQ0NLoGrnVQQZbM-EnD-1J-hOD6vtmUqV9fya1x"

    assert zoom.share_key(play) == zoom.share_key(BROWSER_FORM)


# ------------------------------------- identifying a call without the link

# Zoom's API returns a different share token than its website does, so a pasted
# /rec/share/ link cannot be resolved to a meeting. Not by better matching -
# at all. Everything below is what identifies a call once that is accepted.


def call_meeting(**extra):
    base = {
        "uuid": "abc==", "topic": "Derrick Robison",
        "start_time": "2026-08-19T05:17:00Z",
        "host_email": "santi@agentleadlab.com",
        "recording_files": [transcript_file()],
    }
    return {**base, **extra}


def test_the_passcode_under_the_link_identifies_the_call():
    """Eight random characters. An exact hit is as good as an id."""
    meetings = [
        call_meeting(uuid="other", topic="Someone Else", recording_play_passcode="zzzz"),
        call_meeting(recording_play_passcode="U^M^s7Bw"),
    ]

    found, how = zoom.choose(meetings, link=REAL, passcode="U^M^s7Bw")

    assert found.topic == "Derrick Robison"
    assert how == "its passcode"


def test_a_short_passcode_is_not_trusted_as_an_identifier():
    meetings = [call_meeting(recording_play_passcode="abc")]

    found, how = zoom.choose(meetings, link=REAL, passcode="abc")

    assert how != "its passcode"


def test_the_link_still_wins_when_it_does_match():
    meetings = [call_meeting(share_url=REAL, recording_play_passcode="U^M^s7Bw")]

    assert zoom.choose(meetings, link=REAL, passcode="U^M^s7Bw")[1] == "the link"


def test_the_newest_unfiled_call_is_the_last_resort(monkeypatch):
    import datetime as real_datetime

    class Today(real_datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 21)

    monkeypatch.setattr(zoom, "date", Today)
    meetings = [
        call_meeting(uuid="old", topic="Older", start_time="2026-08-17T10:00:00Z"),
        call_meeting(uuid="new", topic="Newest", start_time="2026-08-19T10:00:00Z"),
    ]

    found, how = zoom.choose(meetings, link=REAL)

    assert found.topic == "Newest"
    assert "not filed yet" in how


def test_a_call_already_filed_is_not_offered_again(monkeypatch):
    import datetime as real_datetime

    class Today(real_datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 21)

    monkeypatch.setattr(zoom, "date", Today)
    meetings = [
        call_meeting(uuid="old", topic="Older", start_time="2026-08-17T10:00:00Z"),
        call_meeting(uuid="new", topic="Newest", start_time="2026-08-19T10:00:00Z"),
    ]

    found, _ = zoom.choose(meetings, link=REAL, filed={"new"})

    assert found.topic == "Older"


def test_a_recording_with_no_transcript_is_never_the_fallback():
    """Picking one would file a card with no summary and no explanation."""
    meetings = [call_meeting(recording_files=[])]

    assert zoom.choose(meetings, link=REAL) == (None, "")


def test_nothing_recent_enough_means_no_guess(monkeypatch):
    import datetime as real_datetime

    class Today(real_datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 12, 1)

    monkeypatch.setattr(zoom, "date", Today)

    assert zoom.choose([call_meeting()], link=REAL) == (None, "")


def test_the_meeting_carries_its_own_id():
    assert zoom.as_recording(call_meeting()).uid == "abc=="
