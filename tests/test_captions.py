"""Caption parsing for the free transcript fallbacks."""

from pathlib import Path

import pytest

from wilbyte import youtube
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


# ------------------------------------------------------------------- cookies


def test_no_cookies_configured_is_none(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)

    assert youtube.cookie_file() is None


def test_cookie_contents_are_written_to_a_file(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv(
        "YOUTUBE_COOKIES",
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc123\n",
    )

    path = youtube.cookie_file()

    assert path is not None
    assert "SID\tabc123" in Path(path).read_text()


def test_a_header_is_added_when_the_paste_is_missing_one(monkeypatch):
    """yt-dlp rejects a cookie file without the Netscape header line."""
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")

    text = Path(youtube.cookie_file()).read_text()

    assert text.startswith("# Netscape HTTP Cookie File")


def test_escaped_newlines_are_restored(monkeypatch):
    """Some hosts flatten a pasted multi-line value into literal backslash-n."""
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", ".youtube.com\\tTRUE\\t/\\tTRUE\\t0\\tSID\\tabc\\nfoo")

    text = Path(youtube.cookie_file()).read_text()

    assert "\n" in text.strip()


def test_whitespace_only_cookies_are_ignored(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", "   \n  ")

    assert youtube.cookie_file() is None


def test_ydl_options_carry_the_cookie_file(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")

    opts = youtube._ydl_opts(writesubtitles=True)

    assert opts["cookiefile"] == youtube.cookie_file()
    assert opts["writesubtitles"] is True
    assert opts["quiet"] is True


def test_ydl_options_omit_cookies_when_there_are_none(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)

    assert "cookiefile" not in youtube._ydl_opts()


def test_spaces_are_turned_back_into_tabs(monkeypatch):
    """Pasting a cookies.txt through a web form often eats the tabs.

    yt-dlp then reads a file with no usable cookies and fails exactly as if
    none had been given.
    """
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv(
        "YOUTUBE_COOKIES",
        ".youtube.com TRUE / TRUE 1799999999 __Secure-3PSID abc123",
    )

    text = Path(youtube.cookie_file()).read_text()

    assert ".youtube.com\tTRUE\t/\tTRUE\t1799999999\t__Secure-3PSID\tabc123" in text


def test_a_comment_line_is_left_alone(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", "# This is a generated file!  Do not edit.\n")

    assert "# This is a generated file!  Do not edit." in Path(youtube.cookie_file()).read_text()


def test_a_signed_in_export_is_recognised(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv(
        "YOUTUBE_COOKIES",
        ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tabc\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf1=50\n",
    )

    assert youtube.cookie_summary() == (2, True)


def test_a_logged_out_export_is_caught(monkeypatch):
    """A valid file with no session in it looks identical until a fetch fails."""
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YOUTUBE_COOKIES", ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf1=50\n")

    assert youtube.cookie_summary() == (1, False)


def test_no_cookies_summarise_as_nothing(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)

    assert youtube.cookie_summary() == (0, False)


# ----------------------------------------------------------- subtitle picking


def fmt(ext, url="https://x/track"):
    return {"ext": ext, "url": url}


def test_human_written_captions_beat_auto_generated():
    info = {
        "subtitles": {"en": [fmt("vtt", "https://x/human")]},
        "automatic_captions": {"en": [fmt("vtt", "https://x/asr")]},
    }

    assert youtube.pick_subtitle_track(info, ("en",)) == ("https://x/human", False)


def test_auto_generated_is_used_when_that_is_all_there_is():
    info = {"automatic_captions": {"en": [fmt("vtt", "https://x/asr")]}}

    assert youtube.pick_subtitle_track(info, ("en",)) == ("https://x/asr", True)


def test_a_wanted_language_wins():
    info = {"subtitles": {
        "es": [fmt("vtt", "https://x/es")],
        "en": [fmt("vtt", "https://x/en")],
    }}

    assert youtube.pick_subtitle_track(info, ("en",))[0] == "https://x/en"


def test_a_regional_variant_counts_as_the_language():
    info = {"subtitles": {"en-GB": [fmt("vtt", "https://x/gb")]}}

    assert youtube.pick_subtitle_track(info, ("en",))[0] == "https://x/gb"


def test_a_parseable_format_is_chosen_over_youtubes_own():
    """json3 and srv3 would each need their own reader; vtt we can already read."""
    info = {"subtitles": {"en": [
        fmt("json3", "https://x/json"), fmt("srv1", "https://x/srv"), fmt("vtt", "https://x/vtt"),
    ]}}

    assert youtube.pick_subtitle_track(info, ("en",))[0] == "https://x/vtt"


def test_no_readable_format_is_no_track():
    info = {"subtitles": {"en": [fmt("json3")]}}

    assert youtube.pick_subtitle_track(info, ("en",)) is None


def test_a_video_with_no_captions_at_all():
    assert youtube.pick_subtitle_track({}, ("en",)) is None


# ------------------------------------------------- retrying across player clients


class FakeYDL:
    """Stands in for yt-dlp, answering differently per player client.

    Modelled on the real behaviour: whether a response carries caption tracks
    varies by client and between requests, so one empty answer proves nothing.
    """

    def __init__(self, by_client):
        self.by_client = by_client
        self.asked = []

    def __call__(self, opts):
        clients = (opts.get("extractor_args", {}).get("youtube", {}) or {}).get("player_client")
        self.client = clients[0] if clients else None
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.asked.append(self.client)
        return self.by_client.get(self.client, {})


def install(monkeypatch, fake):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
    monkeypatch.setattr(
        youtube.httpx, "get",
        lambda *a, **k: SimpleResponse("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello there\n"),
    )


class SimpleResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_an_empty_first_answer_retries_another_client(monkeypatch):
    fake = FakeYDL({
        None: {},
        "web": {"subtitles": {"en": [{"ext": "vtt", "url": "https://x/t"}]}},
    })
    install(monkeypatch, fake)

    result = youtube.fetch_transcript_via_ytdlp("vid123")

    assert "hello there" in result.text
    assert fake.asked == [None, "web"]


def test_the_first_client_that_answers_wins(monkeypatch):
    fake = FakeYDL({None: {"subtitles": {"en": [{"ext": "vtt", "url": "https://x/t"}]}}})
    install(monkeypatch, fake)

    youtube.fetch_transcript_via_ytdlp("vid123")

    assert fake.asked == [None]  # no needless extra requests


def test_every_client_coming_back_empty_says_which_were_tried(monkeypatch):
    install(monkeypatch, FakeYDL({}))

    with pytest.raises(youtube.IngestError) as caught:
        youtube.fetch_transcript_via_ytdlp("vid123")

    message = str(caught.value)
    assert "No caption track published" in message
    for client in ("default", "web", "mweb", "android"):
        assert client in message


# ----------------------------------------------------- a signed-in transcript API


def test_no_cookies_means_no_session(monkeypatch):
    monkeypatch.delenv("YOUTUBE_COOKIES", raising=False)
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)

    assert youtube.cookie_session() is None


def test_the_session_carries_the_cookies_and_a_real_user_agent(monkeypatch):
    """A default python-requests user agent is itself a bot signal."""
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setenv(
        "YOUTUBE_COOKIES",
        ".youtube.com\tTRUE\t/\tTRUE\t1799999999\t__Secure-3PSID\tabc123\n",
    )

    session = youtube.cookie_session()

    assert session is not None
    assert any(c.name == "__Secure-3PSID" for c in session.cookies)
    assert "Mozilla/5.0" in session.headers["User-Agent"]


def test_an_unreadable_cookie_file_yields_no_session(monkeypatch, tmp_path):
    bad = tmp_path / "cookies.txt"
    bad.write_text("this is not a cookie file at all\n")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(bad))

    assert youtube.cookie_session() is None
