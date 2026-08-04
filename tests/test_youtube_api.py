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


# ------------------------------------------------------- half-finished setup


def test_nothing_set_reports_all_three_missing():
    assert youtube_api.missing_oauth_vars() == list(youtube_api.OAUTH_VARS)


def test_the_one_missing_variable_is_named(monkeypatch):
    """Two of three behaves like none, so the check has to say which is absent."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")

    assert youtube_api.missing_oauth_vars() == ["GOOGLE_REFRESH_TOKEN"]


def test_a_blank_value_counts_as_missing(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "   ")

    assert youtube_api.missing_oauth_vars() == ["GOOGLE_REFRESH_TOKEN"]


def test_complete_setup_is_missing_nothing(monkeypatch):
    for name in youtube_api.OAUTH_VARS:
        monkeypatch.setenv(name, "x")

    assert youtube_api.missing_oauth_vars() == []


# ------------------------------------------------------ transcript diagnosis


def test_every_route_that_failed_is_named(monkeypatch):
    """Routes fail for unrelated reasons, so one message has to carry them all.

    Reporting only the first failure meant a cookie problem was invisible
    behind the Data API's permission error, and vice versa.
    """
    from wilbyte import youtube

    for name in youtube_api.OAUTH_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("YOUTUBE_COOKIES", ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    monkeypatch.setattr(
        youtube, "fetch_transcript_via_api",
        lambda *a, **k: (_ for _ in ()).throw(
            youtube_api.YouTubeAPIError("403: caption track not owned by requester")
        ),
    )
    monkeypatch.setattr(
        youtube, "fetch_transcript_via_ytdlp",
        lambda *a, **k: (_ for _ in ()).throw(youtube.IngestError("cookies were rejected")),
    )

    with pytest.raises(youtube.IngestError) as caught:
        youtube.fetch_transcript("vid123", attempts=1)

    message = str(caught.value)
    assert "Data API: 403: caption track not owned by requester" in message
    assert "cookies: cookies were rejected" in message
    # ...and the permission refusal points at the route that actually works.
    assert "cookies are the route that works" in message


def test_the_message_survives_having_no_failures_to_report():
    from wilbyte.youtube import _transcript_failure

    assert "vid123" in _transcript_failure("vid123", [])


# ---------------------------------------------------------- track fallthrough


def test_ranking_puts_the_best_track_first_and_keeps_the_rest():
    """Every track comes back, so a refused download can try the next one."""
    tracks = [track("asr", "en", "ASR"), track("es", "es"), track("human", "en")]

    ranked = youtube_api.rank_caption_tracks(tracks, ("en",))

    assert [t["id"] for t in ranked] == ["human", "es", "asr"]


def test_ranking_of_nothing_is_nothing():
    assert youtube_api.rank_caption_tracks([], ("en",)) == []


def test_a_refused_track_falls_through_to_the_next_one(monkeypatch):
    """captions.download applies a stricter rule than captions.list.

    A track the API happily listed can still 403, so the human-written one
    being refused must not end the attempt while an ASR track remains.
    """
    from wilbyte import youtube

    monkeypatch.setattr(
        youtube_api, "list_captions",
        lambda _vid: [track("human", "en"), track("asr", "en", "ASR")],
    )

    def download(caption_id, **kwargs):
        if caption_id == "human":
            raise youtube_api.YouTubeAPIError("403: not owned by requester")
        return "1\n00:00:01,000 --> 00:00:02,000\nthe aged lead strategy\n"

    monkeypatch.setattr(youtube_api, "download_caption", download)

    result = youtube.fetch_transcript_via_api("vid123")

    assert "aged lead strategy" in result.text
    assert result.source == "youtube-api-asr"


def test_every_track_refused_reports_the_api_error(monkeypatch):
    from wilbyte import youtube

    monkeypatch.setattr(youtube_api, "list_captions", lambda _vid: [track("human", "en")])
    monkeypatch.setattr(
        youtube_api, "download_caption",
        lambda *a, **k: (_ for _ in ()).throw(
            youtube_api.YouTubeAPIError("403: not owned by requester")
        ),
    )

    with pytest.raises(youtube.IngestError, match="not owned by requester"):
        youtube.fetch_transcript_via_api("vid123")


# ------------------------------------------------------- refresh-token errors


class FakeResponse:
    def __init__(self, text, status_code=400):
        self.text = text
        self.status_code = status_code


def test_a_token_from_a_different_client_says_so():
    """unauthorized_client is a credentials mismatch, not an ownership problem.

    Reporting it as "the wrong Google account owns this channel" costs an hour
    of re-consenting that fixes nothing.
    """
    message = youtube_api._explain_refresh(
        FakeResponse('{"error": "unauthorized_client", "error_description": "Unauthorized"}', 401)
    )

    assert "does not belong to this GOOGLE_CLIENT_ID" in message
    assert "Use your own OAuth credentials" in message
    assert "own the channel" not in message


def test_a_dead_token_says_to_mint_a_new_one():
    message = youtube_api._explain_refresh(FakeResponse('{"error": "invalid_grant"}', 400))

    assert "no longer valid" in message
    assert "ya29" in message  # the pasted-the-access-token mistake


def test_an_unrecognised_rejection_still_reports_the_body():
    message = youtube_api._explain_refresh(FakeResponse('{"error": "teapot"}', 418))

    assert "teapot" in message
    assert "418" in message
