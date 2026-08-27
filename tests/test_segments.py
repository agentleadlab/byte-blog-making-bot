"""Cutting an interview into clips."""

import pytest

from wilbyte import segments, youtube
from wilbyte.bot import mentions
from wilbyte.segments import (
    ALWAYS_TAGGED,
    BOILERPLATE,
    MIN_SECONDS,
    SECTIONS,
    Segment,
    SegmentError,
    as_marked_transcript,
    opening,
    parse_segments,
)
from wilbyte.youtube import Cue, length_of, parse_timed_captions, seconds_of, timestamp

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
00:01:03,120 --> 00:01:06,480
and fresh leads are the bike
"""


# --- the cue parser ---------------------------------------------------------


def test_vtt_cues_keep_their_times():
    cues = parse_timed_captions(VTT)
    assert [c.text for c in cues] == [
        "there is a blueprint for hitting",
        "$40,000 a month",
        "and most agents never find it",
    ]
    assert cues[0].start == 0
    assert cues[1].start == pytest.approx(3.12)
    assert cues[2].start == pytest.approx(6.48)


def test_a_scrolling_repeat_keeps_the_earlier_start():
    """The words began when they first appeared, not when the cue repeated."""
    cues = parse_timed_captions(VTT)
    assert cues[0].start == 0
    # ...and the repeat extends the end rather than adding a duplicate line.
    assert cues[0].end == pytest.approx(6.48)


def test_srt_cue_numbers_are_not_dialogue():
    cues = parse_timed_captions(SRT)
    assert [c.text for c in cues] == [
        "aged leads are the training wheels",
        "and fresh leads are the bike",
    ]
    assert cues[1].start == pytest.approx(63.12)


def test_inline_tags_are_stripped():
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>hello</c> there\n"
    assert parse_timed_captions(raw)[0].text == "hello there"


def test_captions_with_no_hour_field():
    raw = "WEBVTT\n\n04:32.000 --> 04:40.000\nhalfway in\n"
    assert parse_timed_captions(raw)[0].start == pytest.approx(272.0)


def test_an_empty_file_has_no_cues():
    assert parse_timed_captions("WEBVTT\n\n") == []


def test_clock_conversions():
    assert seconds_of("00:04:32.120") == pytest.approx(272.12)
    assert seconds_of("00:00:03,120") == pytest.approx(3.12)
    assert timestamp(272.12) == "00:04:32"
    assert timestamp(0) == "00:00:00"
    assert length_of(2346) == "39:06"
    assert length_of(3731) == "1:02:11"
    assert length_of(65) == "1:05"


def test_the_blog_pipeline_still_gets_flat_prose():
    """The two readers coexist - adding timings must not change the old one."""
    assert youtube.parse_captions(VTT) == (
        "there is a blueprint for hitting $40,000 a month "
        "and most agents never find it"
    )


# --- marking up the transcript ----------------------------------------------


def test_the_model_is_shown_timestamps_to_anchor_on():
    cues = [Cue(start=i * 3, end=i * 3 + 3, text=f"line {i}") for i in range(6)]
    marked = as_marked_transcript(cues, every=3)
    assert marked.splitlines()[0] == "[00:00:00] line 0"
    assert marked.splitlines()[1] == "line 1"
    assert marked.splitlines()[3] == "[00:00:09] line 3"


# --- reading the model's answer ---------------------------------------------


def _raw(start, end, **extra):
    payload = {
        "kind": "segment",
        "start": start,
        "end": end,
        "yt_title": "How Jonny Went All-In at 20",
        "website_section": "Agent Success Full Interviews",
        "hook": "Jonny sold $64,000 in pest control his first month.",
        "bullets": ["Why he skipped aged leads", "His full schedule", "The close"],
        "hashtags": ["insuranceagents", "veteranleads"],
        "website_description": "Jonny shares how he went all-in.",
    }
    payload.update(extra)
    return payload


def test_a_short_segment_is_kept_back_not_published():
    keep, short = parse_segments({
        "segments": [_raw("00:00:00", "00:08:00"), _raw("00:08:00", "00:11:40")],
    })
    assert len(keep) == 1
    assert len(short) == 1
    assert short[0].seconds < MIN_SECONDS


def test_the_long_form_entry_is_never_dropped_for_length():
    """It is the whole interview - if it were short, so was the video."""
    keep, short = parse_segments({
        "segments": [_raw("00:00:00", "00:02:00", kind="long-form")],
    })
    assert short == []
    assert keep[0].long_form


def test_everything_too_short_is_an_error_not_an_empty_answer():
    with pytest.raises(SegmentError, match="nothing to publish"):
        parse_segments({"segments": [_raw("00:00:00", "00:03:00")]})


def test_no_segments_at_all_is_an_error():
    with pytest.raises(SegmentError, match="No segments"):
        parse_segments({"segments": []})


def test_a_segment_that_ends_before_it_starts_is_refused():
    with pytest.raises(SegmentError, match="ends at or before"):
        parse_segments({"segments": [_raw("00:10:00", "00:04:00")]})


def test_a_curled_apostrophe_still_names_a_real_section():
    keep, _ = parse_segments({
        "segments": [_raw("00:00:00", "00:08:00", website_section="Agent’s Expectations")],
    })
    assert keep[0].website_section == "Agent's Expectations"


def test_a_section_that_is_not_on_the_website_is_refused():
    with pytest.raises(SegmentError, match="not one of the website sections"):
        parse_segments({
            "segments": [_raw("00:00:00", "00:08:00", website_section="Cold Calling")],
        })


def test_the_house_hashtag_is_always_last_and_never_doubled():
    keep, _ = parse_segments({
        "segments": [_raw("00:00:00", "00:08:00", hashtags=["#AgentLeadLab", "newagent"])],
    })
    assert keep[0].hashtags == ["newagent", ALWAYS_TAGGED]


def test_bullet_characters_the_model_added_back_are_stripped():
    keep, _ = parse_segments({
        "segments": [_raw("00:00:00", "00:08:00", bullets=["• Why he skipped aged leads."])],
    })
    assert keep[0].bullets == ["Why he skipped aged leads"]


# --- what gets pasted -------------------------------------------------------


def test_the_links_come_from_the_code_not_the_model():
    keep, _ = parse_segments({"segments": [_raw("00:00:36", "00:08:16")]})
    text = keep[0].as_text()
    assert BOILERPLATE in text
    assert "https://agentleadlab.com/strategysession" in text
    assert "https://www.instagram.com/agentleadlab_/" in text


def test_an_entry_reads_the_way_the_doc_wants_it():
    keep, _ = parse_segments({"segments": [_raw("00:00:36", "00:08:16")]})
    text = keep[0].as_text()
    assert text.startswith("SEGMENT (00:00:36–00:08:16) — 7:40")
    assert "(YT Title)" in text
    assert "Agent Success Full Interviews (Website section)" in text
    assert "(YT Description)" in text
    assert "(Website Description)" in text
    assert "• Why he skipped aged leads" in text
    # The website description is last, so the hashtags sit inside the YT block.
    assert text.rstrip().endswith("Jonny shares how he went all-in.")
    assert "#insuranceagents #veteranleads #agentleadlab" in text


def test_the_long_form_entry_says_so():
    keep, _ = parse_segments({
        "segments": [_raw("00:04:32", "00:43:38", kind="long-form")],
    })
    assert keep[0].heading == "LONG-FORM / FULL INTERVIEW (00:04:32–00:43:38) — 39:06"


def test_a_missing_closing_line_leaves_no_blank_gap():
    keep, _ = parse_segments({"segments": [_raw("00:00:00", "00:08:00", closing="")]})
    assert "\n\n\n" not in keep[0].as_text()


def test_the_opening_counts_clips_and_names_what_was_dropped():
    short = [Segment(start=0, end=200, yt_title="x", website_section=SECTIONS[0], hook="")]
    text = opening({"summary": "Jonny.", "pull_quote": "Speed."}, kept=3, short=short)
    assert "Jonny." in text
    assert '"Speed."' in text
    assert "**2 segments** plus the full interview." in text
    assert "00:00:00–00:03:20 (3:20)" in text


def test_one_clip_is_not_pluralised():
    assert "**1 segment** plus" in opening({}, kept=2, short=[])


# --- asking for it ----------------------------------------------------------


def test_segment_plus_a_link_asks_for_clips():
    request = mentions.parse("<@1> segment https://youtu.be/abcdefghijk")
    assert request.action == "segments"
    assert request.source == "https://youtu.be/abcdefghijk"


@pytest.mark.parametrize("word", ["segment", "segments", "clips", "chapters", "cut"])
def test_the_other_ways_of_saying_it(word):
    assert mentions.parse(f"<@1> {word} https://youtu.be/abcdefghijk").action == "segments"


def test_a_link_on_its_own_still_writes_a_blog_post():
    """The verb is what separates the two - a link alone has always meant a post."""
    assert mentions.parse("<@1> https://youtu.be/abcdefghijk").action == "run"


def test_segment_in_a_copy_brief_is_not_a_command():
    """'segment your audience' is somebody asking for an email, not a transcript."""
    request = mentions.parse("<@1> email about how to segment your audience")
    assert request.action == "write"
    assert request.format_key == "email"


def test_asking_with_no_link_at_all():
    assert mentions.parse("<@1> segment").action == "segments"
    assert mentions.parse("<@1> segment").source is None
