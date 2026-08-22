"""Reading what was actually said in a Loom video.

A Loom SOP was summarised as "this entry is a placeholder, watch the video" -
four paragraphs about having nothing to say. Loom captions every video and
serves the captions publicly, so there was something to read all along.
"""

from __future__ import annotations

import httpx
import pytest

from wilbyte import loom

SHARE = "https://www.loom.com/share/e0c1eb6c45ba40758b928803dffd4eaa"
ID = "e0c1eb6c45ba40758b928803dffd4eaa"

VTT = """WEBVTT

1
00:00:00.000 --> 00:00:04.120
So the first thing you do is open the VA tracker.

2
00:00:04.120 --> 00:00:08.000
So the first thing you do is open the VA tracker.

3
00:00:08.000 --> 00:00:12.500
Then you filter by <b>owner</b> and check last week's column.
"""


# ------------------------------------------------------ finding the video


@pytest.mark.parametrize(
    "url",
    [
        SHARE,
        f"https://loom.com/share/{ID}",
        f"https://www.loom.com/embed/{ID}",
        f"https://www.loom.com/share/{ID}?sid=abc-123",
        f"https://www.loom.com/share/{ID.upper()}",
    ],
)
def test_the_id_comes_out_of_any_shape_of_link(url):
    assert loom.video_id(url) == ID


@pytest.mark.parametrize(
    "url", ["", "https://youtu.be/abc", "https://www.loom.com/", "not a url"]
)
def test_something_that_is_not_a_loom_link_has_no_id(url):
    assert loom.video_id(url) == ""


# --------------------------------------------------- reading the captions


def test_the_words_come_out_without_the_timings():
    said = loom.spoken(VTT)

    assert said.startswith("So the first thing you do is open the VA tracker.")
    assert "-->" not in said
    assert "WEBVTT" not in said


def test_a_repeated_cue_is_not_said_twice():
    """Captions repeat the last line as often as not, and a summary written
    from a transcript that stutters reads like one."""
    assert loom.spoken(VTT).count("open the VA tracker") == 1


def test_the_markup_inside_a_cue_is_dropped():
    assert "<b>" not in loom.spoken(VTT)
    assert "owner" in loom.spoken(VTT)


def test_srt_numbering_is_not_mistaken_for_speech():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nOpen the sheet.\n\n2\n" \
          "00:00:03,000 --> 00:00:05,000\nFilter to last week.\n"

    assert loom.spoken(srt) == "Open the sheet. Filter to last week."


def test_nothing_in_means_nothing_out():
    assert loom.spoken("") == ""
    assert loom.spoken("WEBVTT\n\n") == ""


# ------------------------------------------------------ talking to Loom


class FakeLoom:
    """Stands in for Loom's GraphQL API and its caption files."""

    def __init__(self, *, transcript=None, video=None, captions=VTT, status=200):
        self.transcript = transcript
        self.video = video
        self.captions = captions
        self.status = status
        self.operations = []

    def post(self, url, *, json, headers, timeout):
        self.operations.append((json["operationName"], json["variables"], headers))
        if self.status >= 400:
            return httpx.Response(self.status, request=httpx.Request("POST", url))
        data = (
            {"fetchVideoTranscript": self.transcript}
            if json["operationName"] == "FetchVideoTranscript"
            else {"getVideo": self.video}
        )
        return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))

    def get(self, url, *, timeout, follow_redirects):
        return httpx.Response(200, text=self.captions, request=httpx.Request("GET", url))


@pytest.fixture
def fake(monkeypatch):
    def _use(**kwargs):
        stub = FakeLoom(**kwargs)
        monkeypatch.setattr(loom.httpx, "post", stub.post)
        monkeypatch.setattr(loom.httpx, "get", stub.get)
        return stub

    return _use


def test_a_public_video_reads_end_to_end(fake):
    fake(transcript={"captions_source_url": "https://cdn.loom.com/x.vtt"})

    assert "open the VA tracker" in loom.transcript(SHARE)


def test_the_older_field_name_is_accepted_too(fake):
    """Loom answers with one or the other depending on the video."""
    fake(transcript={"source_url": "https://cdn.loom.com/x.vtt"})

    assert loom.transcript(SHARE)


def test_loom_is_asked_about_the_right_video(fake):
    stub = fake(transcript={"captions_source_url": "https://cdn.loom.com/x.vtt"})

    loom.transcript(SHARE)

    operation, variables, headers = stub.operations[0]
    assert operation == "FetchVideoTranscript"
    assert variables["videoId"] == ID
    assert headers["apollographql-client-name"] == "web"


def test_a_private_video_says_so_rather_than_looking_like_a_success(fake):
    """Loom returns its refusals in the body with a 200, so a private video
    arrives looking like it worked."""
    fake(transcript={"message": "You do not have access to this video"})

    with pytest.raises(loom.LoomError, match="do not have access"):
        loom.transcript(SHARE)


def test_a_video_with_no_captions_yet_is_empty_not_an_error():
    """Too new to have been processed, or nobody spoke. Neither is a failure -
    it is a video with nothing to read."""
    import wilbyte.loom as module

    def no_captions(url, *, json, headers, timeout):
        return httpx.Response(
            200, json={"data": {"fetchVideoTranscript": {"captions_source_url": ""}}},
            request=httpx.Request("POST", url),
        )

    original = module.httpx.post
    module.httpx.post = no_captions
    try:
        assert module.transcript(SHARE) == ""
    finally:
        module.httpx.post = original


def test_a_link_that_is_not_a_loom_is_refused_before_any_request():
    with pytest.raises(loom.LoomError, match="isn't a Loom share link"):
        loom.transcript("https://youtu.be/abc12345678")


def test_loom_being_down_is_reported_as_such(fake):
    fake(status=503)

    with pytest.raises(loom.LoomError, match="503"):
        loom.transcript(SHARE)


def test_the_title_comes_back_for_the_card(fake):
    fake(video={"name": "Streamlining VA Team Operations  "})

    assert loom.title(SHARE) == "Streamlining VA Team Operations"


def test_a_title_that_cannot_be_read_is_empty_rather_than_raised(fake):
    """The card still files under whatever else it has. Losing the whole SOP
    over its name would be the wrong trade."""
    fake(status=500)

    assert loom.title(SHARE) == ""


# ------------------------------------------ what the card says when it can't


def test_a_link_nobody_could_read_gets_one_line_not_four_paragraphs(config, monkeypatch):
    """The card said, at length, that it was a placeholder and somebody should
    watch the video. What a search matches on is the summary, and an apology
    matches nothing anybody would type."""
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "describe_page", lambda url, **kw: "Some Page\nA description")
    monkeypatch.setattr(
        jobs, "write_sop_summary", lambda *a, **kw: pytest.fail("nothing was read to summarise")
    )
    sop = sops.find_sop("https://example.com/how-we-do-it")

    summary = jobs.sop_summary(config, sop)

    assert summary.count(".") <= 3, summary
    assert sop.title in summary


def test_a_loom_that_reads_is_summarised_properly(config, monkeypatch):
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "write_sop_summary", lambda config, sop, material: material)
    monkeypatch.setattr(loom, "transcript", lambda url, **kw: "Open the VA tracker, then filter.")
    monkeypatch.setattr(loom, "title", lambda url, **kw: "Streamlining VA Team Operations")

    sop = sops.find_sop(SHARE)
    summary = jobs.sop_summary(config, sop)

    assert "Open the VA tracker" in summary
    assert sop.title == "Streamlining VA Team Operations"


def test_a_private_loom_says_what_to_do_about_it(config, monkeypatch):
    """"No summary" is not actionable. "Share it with anyone-with-the-link" is."""
    from wilbyte import sops
    from wilbyte.bot import jobs

    def refused(url, **kwargs):
        raise loom.LoomError("You do not have access to this video")

    monkeypatch.setattr(loom, "transcript", refused)
    sop = sops.find_sop(SHARE)

    jobs.sop_summary(config, sop)

    assert "anyone-with-the-link" in sop.note


def test_a_heading_somebody_typed_is_not_overwritten_by_loom(config, monkeypatch):
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "write_sop_summary", lambda *a, **kw: "x")
    monkeypatch.setattr(loom, "transcript", lambda url, **kw: "words")
    monkeypatch.setattr(loom, "title", lambda url, **kw: "Loom's Own Name")

    sop = sops.find_sop(f"**How We Run VA Standups**\n{SHARE}")
    jobs.sop_summary(config, sop)

    assert sop.title == "How We Run VA Standups"


# ------------------------------------------------- what the card gets called


@pytest.mark.parametrize(
    "summary,expected",
    [
        ("**VA Team: Campaign Launch & Lead Delivery Monitoring**\n\nThis covers…",
         "VA Team: Campaign Launch & Lead Delivery Monitoring"),
        ("## How To Upload Blog Posts\n\nSteps…", "How To Upload Blog Posts"),
        ("**Lead Order Process:**\n- open the sheet", "Lead Order Process"),
        ("\n\n**Weekly Numbers**\ntext", "Weekly Numbers"),
    ],
)
def test_the_summary_names_the_card_when_nobody_else_did(summary, expected):
    from wilbyte import sops

    assert sops.headline(summary) == expected


@pytest.mark.parametrize(
    "summary",
    [
        "",
        "This entry covers what the VA team is responsible for.",
        "- open the sheet\n- filter to last week",
        "**bold** in the middle of a sentence is not a heading",
    ],
)
def test_prose_is_not_mistaken_for_a_heading(summary):
    from wilbyte import sops

    assert sops.headline(summary) == ""


def test_a_loom_nobody_titled_ends_up_named_for_what_it_covers(config, monkeypatch):
    """It filed as "SOP: Loom SOP" - the placeholder. The video had been read
    by then, so there was a far better name available."""
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(loom, "transcript", lambda url, **kw: "So once a client pays…")
    monkeypatch.setattr(loom, "title", lambda url, **kw: "")
    monkeypatch.setattr(jobs, "describe_page", lambda url, **kw: "")
    monkeypatch.setattr(
        jobs, "write_sop_summary",
        lambda *a, **kw: "**VA Team: Campaign Launch & Lead Delivery Monitoring**\n\nCovers…",
    )

    sop = sops.find_sop(SHARE)
    assert sop.title == "Loom SOP", "the placeholder, before anything is read"

    jobs.sop_summary(config, sop)

    assert sops.card_title(sop) == (
        "SOP: VA Team: Campaign Launch & Lead Delivery Monitoring"
    )


def test_the_share_page_name_is_used_when_loom_will_not_say(config, monkeypatch):
    """The GraphQL name query is the least reliable of the three. og: tags on
    the share page are not."""
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(loom, "transcript", lambda url, **kw: "words")
    monkeypatch.setattr(loom, "title", lambda url, **kw: "")
    monkeypatch.setattr(jobs, "describe_page", lambda url, **kw: "Streamlining VA Operations")
    monkeypatch.setattr(jobs, "write_sop_summary", lambda *a, **kw: "no heading here")

    sop = sops.find_sop(SHARE)
    jobs.sop_summary(config, sop)

    assert sop.title == "Streamlining VA Operations"


def test_a_heading_somebody_typed_still_beats_all_three(config, monkeypatch):
    from wilbyte import sops
    from wilbyte.bot import jobs

    monkeypatch.setattr(loom, "transcript", lambda url, **kw: "words")
    monkeypatch.setattr(loom, "title", lambda url, **kw: "Loom's Name")
    monkeypatch.setattr(jobs, "write_sop_summary", lambda *a, **kw: "**The Summary's Name**\nx")

    sop = sops.find_sop(f"**How We Run VA Standups**\n{SHARE}")
    jobs.sop_summary(config, sop)

    assert sop.title == "How We Run VA Standups"
