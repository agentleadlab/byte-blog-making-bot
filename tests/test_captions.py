"""Caption parsing for the free transcript fallbacks."""

from pathlib import Path

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
