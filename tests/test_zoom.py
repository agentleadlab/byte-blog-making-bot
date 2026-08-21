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
