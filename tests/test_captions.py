"""Caption parsing for the free transcript fallbacks."""

from wilbyte.youtube import clean_transcript, parse_captions

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:03.120
there is a blueprint for hitting

00:00:03.120 --> 00:00:06.480
there is a blueprint for hitting
$40,000 a month

00:00:06.480 --> 00:00:09.000
and most agents never find it
"""

SRT = """1
00:00:00,000 --> 00:00:03,120
aged leads are the training wheels

2
00:00:03,120 --> 00:00:06,480
of this business
"""


def test_vtt_becomes_clean_prose():
    assert parse_captions(VTT) == (
        "there is a blueprint for hitting $40,000 a month and most agents never find it"
    )


def test_scrolling_duplicates_are_collapsed():
    """Auto-captions repeat the previous line as new words scroll in."""
    text = parse_captions(VTT)

    assert text.count("there is a blueprint") == 1


def test_srt_is_handled_too():
    assert parse_captions(SRT) == "aged leads are the training wheels of this business"


def test_headers_and_cue_numbers_are_dropped():
    text = parse_captions(VTT)

    assert "WEBVTT" not in text
    assert "Kind:" not in text
    assert "-->" not in text


def test_inline_karaoke_tags_are_stripped():
    raw = (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n"
        "<00:00:00.320><c>aged</c> <00:00:00.800><c>leads</c> work\n"
    )

    assert parse_captions(raw) == "aged leads work"


def test_empty_caption_file_yields_nothing():
    assert parse_captions("WEBVTT\n\n") == ""


def test_cleaning_strips_caption_artefacts():
    assert clean_transcript("[Music] hello  there [Applause]") == "hello there"
