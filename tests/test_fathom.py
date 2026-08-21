"""Reading a Fathom-recorded sales call.

Fathom sits in the meeting as a notetaker, so there is no passcode and no
separate transcription setting to have forgotten - if the call is in Fathom,
its transcript exists.

These tests are deliberately tolerant about field names. The integration was
written without a key to try it against, and the last time a response shape was
assumed rather than checked it cost three missed publish days.
"""

import pytest

from wilbyte import fathom

SHARE = "https://fathom.video/share/abc123XYZtoken"


# ------------------------------------------------------------ matching a link


@pytest.mark.parametrize(
    "url",
    [SHARE, SHARE + "/", SHARE + "?utm_source=email", SHARE + "#t=120"],
)
def test_every_dressing_of_a_link_reduces_to_the_same_key(url):
    assert fathom.share_key(url) == fathom.share_key(SHARE)


def test_the_two_link_shapes_are_both_understood():
    """Fathom passes calls round as /share/<token> and /calls/<id>."""
    assert fathom.share_key("https://fathom.video/calls/12345") == "12345"
    assert fathom.share_key("https://fathom.video/share/abc") == "abc"


def test_a_different_call_does_not_match():
    assert fathom.match_share_url([{"url": "https://fathom.video/share/other"}], SHARE) is None


@pytest.mark.parametrize("field", ["url", "share_url", "recording_url", "share_link"])
def test_the_link_is_found_whatever_the_field_is_called(field):
    assert fathom.match_share_url([{field: SHARE, "title": "Sales Call"}], SHARE) is not None


def test_an_empty_link_matches_nothing():
    assert fathom.match_share_url([{"url": ""}], "") is None


# ------------------------------------------------------------------ the call


@pytest.mark.parametrize("field", ["title", "meeting_title", "name", "topic"])
def test_the_title_is_found_whatever_the_field_is_called(field):
    call = fathom.as_call({field: "Ryan Egert - Agent Lead Lab", "url": SHARE})

    assert call.title == "Ryan Egert - Agent Lead Lab"


def test_participants_are_collected_from_objects():
    call = fathom.as_call({
        "url": SHARE,
        "invitees": [
            {"name": "Santiago Villegas", "email": "santiago@agentleadlab.com"},
            {"name": "Ryan Egert", "email": "ryan@example.com"},
        ],
    })

    assert call.participants == ("Santiago Villegas", "Ryan Egert")


def test_participants_are_collected_from_plain_strings():
    call = fathom.as_call({"url": SHARE, "participants": ["Santiago Villegas", "Ryan Egert"]})

    assert call.participants == ("Santiago Villegas", "Ryan Egert")


def test_an_invitee_with_no_name_falls_back_to_the_email():
    call = fathom.as_call({"url": SHARE, "invitees": [{"email": "ryan@example.com"}]})

    assert call.participants == ("ryan@example.com",)


def test_the_same_person_listed_twice_appears_once():
    call = fathom.as_call({
        "url": SHARE,
        "invitees": [{"name": "Ryan Egert"}],
        "attendees": [{"name": "Ryan Egert"}],
    })

    assert call.participants == ("Ryan Egert",)


# -------------------------------------------------------------- the transcript


def test_a_transcript_that_arrives_as_a_string():
    assert fathom.transcript_text({"transcript": "  they pushed back on price  "}) == (
        "they pushed back on price"
    )


def test_speaker_turns_are_flattened_with_their_names():
    """Who raised the objection is the useful half of a sales-call summary."""
    text = fathom.transcript_text({
        "transcript": [
            {"speaker": {"display_name": "Santiago"}, "text": "What's holding you back?"},
            {"speaker": {"display_name": "Ryan"}, "text": "The price, honestly."},
        ]
    })

    assert text == "Santiago: What's holding you back?\nRyan: The price, honestly."


def test_a_speaker_given_as_a_plain_name_works_too():
    text = fathom.transcript_text({"segments": [{"speaker_name": "Ryan", "text": "Hello"}]})

    assert text == "Ryan: Hello"


def test_a_turn_with_no_speaker_keeps_its_words():
    assert fathom.transcript_text({"transcript": [{"text": "Hello"}]}) == "Hello"


def test_empty_turns_are_dropped_rather_than_left_as_blank_lines():
    text = fathom.transcript_text({
        "transcript": [{"text": ""}, {"speaker": "Ryan", "text": "Hello"}, {"text": "   "}]
    })

    assert text == "Ryan: Hello"


def test_no_transcript_is_an_empty_answer_not_a_crash():
    assert fathom.transcript_text({"url": SHARE}) == ""


# --------------------------------------------------------------- diagnostics


def test_a_missed_match_can_report_what_came_back():
    """Guessing at a response shape twice is a habit worth breaking."""
    report = fathom.describe([{"id": "1", "meeting_title": "Sales Call", "url": SHARE}])

    assert "1 call(s) visible" in report
    assert "meeting_title" in report
    assert "Sales Call" in report


def test_an_empty_response_says_so_plainly():
    assert "no calls at all" in fathom.describe([])
