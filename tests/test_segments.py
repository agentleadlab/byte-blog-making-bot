"""Cutting an interview into clips."""

import pytest

from wilbyte import fathom, segments, youtube, zoom
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


def test_segment_takes_a_zoom_link():
    request = mentions.parse("<@1> segment https://us02web.zoom.us/rec/share/abc123")
    assert request.action == "segments"
    assert request.source == "https://us02web.zoom.us/rec/share/abc123"


def test_segment_takes_a_fathom_link():
    request = mentions.parse("<@1> clips https://fathom.video/share/xyz789")
    assert request.action == "segments"
    assert request.source == "https://fathom.video/share/xyz789"


def test_the_full_interview_starts_where_the_first_clip_does():
    """The 21 seconds before the first clip are the greetings, not the interview."""
    keep, _ = parse_segments({"segments": [
        _raw("00:00:01", "00:31:55", kind="long-form"),
        _raw("00:00:22", "00:04:26"),
        _raw("00:04:26", "00:11:01"),
    ]})
    assert keep[0].range == "00:00:22–00:31:55"


def test_a_long_opening_is_content_and_is_kept():
    """A first clip ten minutes in means the opening wasn't clippable, not dead."""
    keep, _ = parse_segments({"segments": [
        _raw("00:00:00", "00:31:55", kind="long-form"),
        _raw("00:10:00", "00:16:00"),
    ]})
    assert keep[0].range == "00:00:00–00:31:55"


def test_a_short_segment_is_named_back_to_the_model_with_its_length():
    """Dropping four of seven left holes in the video; folding them in doesn't."""
    _keep, short = parse_segments({
        "segments": [_raw("00:00:00", "00:08:00"), _raw("00:11:01", "00:14:34")],
    })
    note = segments.too_short_note(short)
    assert "00:11:01–00:14:34 (3:33)" in note
    assert "folded into the segment beside it" in note
    assert "Do not simply drop them" in note


def test_the_lengths_of_a_payload_that_is_entirely_too_short():
    """Everything short raises rather than returning, so the retry needs this."""
    payload = {"segments": [_raw("00:00:00", "00:03:00"), _raw("00:03:00", "00:06:00")]}
    with pytest.raises(SegmentError):
        parse_segments(payload)
    assert [s.range for s in segments.parse_lengths(payload)] == [
        "00:00:00–00:03:00", "00:03:00–00:06:00",
    ]


def test_a_failure_can_carry_the_evidence_behind_it():
    """"I couldn't find it" has three causes and they look identical in Discord."""
    exc = SegmentError("Nothing matched.", detail="What Zoom returned: ...")
    assert str(exc) == "Nothing matched."
    assert exc.detail == "What Zoom returned: ..."


def test_an_error_with_nothing_to_add_says_nothing_extra():
    assert SegmentError("Zoom has no transcript for that call.").detail == ""


def test_a_zoom_link_on_its_own_still_files_the_call():
    """The verb is the whole difference - a bare link has always meant 'file this'."""
    assert mentions.parse("<@1> https://us02web.zoom.us/rec/share/abc").action == "recording"


# --- finding a Zoom recording by who is on it -------------------------------


def _meeting(topic, start, **extra):
    payload = {"uuid": topic, "topic": topic, "start_time": start}
    payload.update(extra)
    return payload


ACCOUNT = [
    _meeting("Antonio Bohorquez - llamada con Agent Lead Lab", "2026-08-03T15:00:00Z"),
    _meeting("Arlene Linares", "2026-08-21T15:00:00Z"),
    _meeting("Luke Venzlaff", "2026-08-21T18:00:00Z"),
    _meeting("Weekly Sales Call", "2026-08-24T15:00:00Z"),
    _meeting("Weekly Sales Call", "2026-08-17T15:00:00Z"),
]


def test_a_guest_name_finds_the_recording():
    found = zoom.search_topics(ACCOUNT, "Antonio")
    assert [r.topic for r in found] == ["Antonio Bohorquez - llamada con Agent Lead Lab"]


def test_every_word_has_to_appear_so_a_second_one_narrows():
    assert zoom.search_topics(ACCOUNT, "Arlene Linares")
    assert zoom.search_topics(ACCOUNT, "Arlene Venzlaff") == []


def test_filler_words_in_a_topic_do_not_widen_the_search():
    """'Jonny interview' should find what 'Jonny' finds, not nothing."""
    assert [r.topic for r in zoom.search_topics(ACCOUNT, "Antonio interview call")] == [
        "Antonio Bohorquez - llamada con Agent Lead Lab"
    ]


def test_a_search_of_only_filler_matches_nothing():
    """Otherwise 'the call' would return the whole account."""
    assert zoom.search_topics(ACCOUNT, "the call") == []
    assert zoom.search_topics(ACCOUNT, "") == []


def test_a_repeating_call_comes_back_newest_first():
    found = zoom.search_topics(ACCOUNT, "Weekly")
    assert len(found) == 2
    assert found[0].started_at.startswith("2026-08-24")


def test_the_name_search_ignores_case():
    assert zoom.search_topics(ACCOUNT, "ANTONIO") == zoom.search_topics(ACCOUNT, "antonio")


def test_segment_by_name_carries_the_name_through():
    request = mentions.parse("<@1> segment Antonio Bohorquez")
    assert request.action == "segments"
    assert request.source is None
    assert request.brief == "Antonio Bohorquez"


# --- Fathom's timings -------------------------------------------------------


def test_seconds_come_back_from_whichever_field_fathom_used():
    assert fathom.turn_seconds({"timestamp": 272}) == 272
    assert fathom.turn_seconds({"start_time": "00:04:32"}) == pytest.approx(272.0)
    assert fathom.turn_seconds({"offset": "272.5"}) == pytest.approx(272.5)
    assert fathom.turn_seconds({"speaker": "Tre"}) is None


def test_absolute_timestamps_become_an_offset_from_the_first_turn():
    """A call that started at 2pm must not produce clips at 50,400 seconds."""
    meeting = {"transcript": [
        {"started_at": "2026-08-27T14:00:00Z", "speaker": "Tre", "text": "So walk me through it"},
        {"started_at": "2026-08-27T14:00:30Z", "speaker": "Jonny", "text": "I was knocking doors"},
    ]}
    assert fathom.timed_turns(meeting) == [
        (0.0, "Tre: So walk me through it"),
        (30.0, "Jonny: I was knocking doors"),
    ]


def test_a_turn_with_no_time_on_it_is_left_out_not_guessed():
    meeting = {"transcript": [
        {"timestamp": 0, "speaker": "Tre", "text": "first"},
        {"speaker": "Jonny", "text": "no time on this one"},
        {"timestamp": 20, "speaker": "Tre", "text": "third"},
    ]}
    assert [text for _, text in fathom.timed_turns(meeting)] == ["Tre: first", "Tre: third"]


def test_a_transcript_with_no_timings_at_all_comes_back_empty():
    """An empty answer is what makes the caller say so instead of inventing times."""
    assert fathom.timed_turns({"transcript": [{"speaker": "Tre", "text": "hello"}]}) == []
    assert fathom.timed_turns({"transcript": "Tre: hello"}) == []


def test_the_old_flat_transcript_still_reads_the_same():
    """Adding timings must not change what the call summariser gets."""
    meeting = {"transcript": [
        {"timestamp": 0, "speaker": "Tre", "text": "So walk me through it"},
        {"timestamp": 30, "speaker": "Jonny", "text": "I was knocking doors"},
    ]}
    assert fathom.transcript_text(meeting) == (
        "Tre: So walk me through it\nJonny: I was knocking doors"
    )


# --- Zoom's transcript ------------------------------------------------------


ZOOM_VTT = """WEBVTT

1
00:00:04.320 --> 00:00:09.100
Tre: So walk me through how you got started

2
00:00:09.100 --> 00:00:14.880
Jonny: I was knocking doors for pest control
"""


def test_zoom_speaker_labels_survive_into_the_cues():
    """The labels are how the model finds Tre's questions, so they stay on."""
    cues = parse_timed_captions(ZOOM_VTT)
    assert cues[0].text == "Tre: So walk me through how you got started"
    assert cues[0].start == pytest.approx(4.32)
    assert cues[1].text.startswith("Jonny:")
