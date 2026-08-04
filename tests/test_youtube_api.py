"""YouTube Data API credential handling and caption track selection."""

import pytest

from wilbyte import youtube_api


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "YOUTUBE_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    youtube_api._token_cache.clear()


def track(track_id, language, kind="standard"):
    return {"id": track_id, "snippet": {"language": language, "trackKind": kind}}


# ----------------------------------------------------------------- credentials


def test_nothing_configured():
    assert not youtube_api.configured()
    assert youtube_api.api_key() is None
    assert youtube_api.oauth_credentials() is None


def test_api_key_alone_counts_as_configured(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "abc123")

    assert youtube_api.configured()
    # ...but not for captions, which need OAuth.
    assert youtube_api.oauth_credentials() is None


def test_partial_oauth_is_not_treated_as_configured(monkeypatch):
    """Two of three variables is a half-finished setup, not a usable one."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")

    assert youtube_api.oauth_credentials() is None


def test_complete_oauth(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "refresh")

    assert youtube_api.oauth_credentials() == ("id", "secret", "refresh")
    assert youtube_api.configured()


def test_whitespace_only_values_are_ignored(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "   ")

    assert youtube_api.api_key() is None


def test_access_token_without_oauth_explains_itself():
    with pytest.raises(youtube_api.YouTubeAPIError, match="OAuth is not configured"):
        youtube_api.access_token()


# --------------------------------------------------------------- track picking


def test_human_captions_beat_auto_generated():
    """ASR output is unpunctuated and badly cased - it makes worse copy."""
    tracks = [track("asr", "en", "ASR"), track("human", "en")]

    assert youtube_api.pick_caption_track(tracks, ("en",))["id"] == "human"


def test_auto_generated_is_used_when_that_is_all_there_is():
    tracks = [track("asr", "en", "ASR")]

    assert youtube_api.pick_caption_track(tracks, ("en",))["id"] == "asr"


def test_language_preference_is_respected():
    tracks = [track("es", "es"), track("en", "en")]

    assert youtube_api.pick_caption_track(tracks, ("en",))["id"] == "en"


def test_regional_variants_match_the_base_language():
    tracks = [track("gb", "en-GB")]

    assert youtube_api.pick_caption_track(tracks, ("en",))["id"] == "gb"


def test_any_track_beats_no_track():
    """A Spanish transcript still describes the video; nothing describes nothing."""
    tracks = [track("es", "es")]

    assert youtube_api.pick_caption_track(tracks, ("en",))["id"] == "es"


def test_no_tracks_returns_none():
    assert youtube_api.pick_caption_track([], ("en",)) is None
