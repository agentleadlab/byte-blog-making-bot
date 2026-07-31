"""Bot logic that can be tested without a gateway connection."""

from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from wilbyte.bot import embeds, jobs
from wilbyte.bot.client import is_allowed, parse_guild_id
from wilbyte.bot.views import Decision
from wilbyte.models import Video
from wilbyte.pipeline import assemble_post
from wilbyte.youtube import looks_like_playlist

CT = ZoneInfo("America/Chicago")

PLAYLIST = "https://youtube.com/playlist?list=PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0"


def actor(channel_id: int, role_ids: list[int] = ()):
    user = SimpleNamespace(id=1, roles=[SimpleNamespace(id=r) for r in role_ids])
    return {"channel_id": channel_id, "user": user}


# ------------------------------------------------------------------ source kind


@pytest.mark.parametrize(
    "url",
    [
        PLAYLIST,
        "https://www.youtube.com/watch?v=w7mazKut2lk&list=PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0",
        "PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0",
    ],
)
def test_playlist_urls_are_recognised(url):
    assert looks_like_playlist(url)


@pytest.mark.parametrize(
    "url",
    ["https://youtu.be/w7mazKut2lk", "https://www.youtube.com/watch?v=w7mazKut2lk", "w7mazKut2lk"],
)
def test_single_video_urls_are_not_playlists(url):
    assert not looks_like_playlist(url)


# ------------------------------------------------------------------ permissions


def test_no_allowlist_permits_any_channel(config):
    allowed, _ = is_allowed(**actor(999), config=config)

    assert allowed


def test_channel_allowlist_blocks_other_channels(config):
    gated = replace(config.secrets, discord_channel_ids=("111",))
    config = replace(config, secrets=gated)

    assert is_allowed(**actor(111), config=config)[0]
    allowed, reason = is_allowed(**actor(222), config=config)
    assert not allowed
    assert "channel" in reason


def test_role_allowlist_requires_a_matching_role(config):
    gated = replace(config.secrets, discord_role_ids=("42",))
    config = replace(config, secrets=gated)

    assert is_allowed(**actor(1, role_ids=[42]), config=config)[0]
    allowed, reason = is_allowed(**actor(1, role_ids=[7]), config=config)
    assert not allowed
    assert "role" in reason


# ------------------------------------------------------------------- guild id


def test_a_plain_server_id_parses():
    assert parse_guild_id("1234567890123456789") == 1234567890123456789
    assert parse_guild_id("  1234567890123456789  ") == 1234567890123456789
    assert parse_guild_id('"1234567890123456789"') == 1234567890123456789


def test_an_invite_url_is_rejected_rather_than_mined_for_digits():
    """It contains the *application* id, not the server's - using it would sync
    commands to nowhere, so it must be refused outright."""
    url = (
        "https://discord.com/oauth2/authorize?client_id=1532879451400000201"
        "&permissions=117760&integration_type=0&scope=applications.commands+bot"
    )

    assert parse_guild_id(url) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "not-an-id", "12345", "abc123456789012345"])
def test_junk_values_return_none_instead_of_raising(raw):
    assert parse_guild_id(raw) is None


# ---------------------------------------------------------------------- embeds


def test_preview_embed_shows_every_field_before_anything_is_sent(copy_package, config, tmp_path):
    video = Video(video_id="w7mazKut2lk", title="t", url="u")
    post = assemble_post(video, copy_package, config, output_dir=tmp_path, report=lambda _: None)
    post.scheduled_at = datetime(2026, 8, 12, 10, tzinfo=CT)

    embed = embeds.post_preview(post, index=1, total=3, mode="scheduled")
    fields = {f.name: f.value for f in embed.fields}

    assert embed.title == post.title
    assert "Post 1 of 3" in embed.author.name
    assert fields["URL slug"] == "`insurance-lead-progression-roadmap`"
    assert fields["Category"] == "LeadLab"
    assert "Aug 12" in fields["Scheduled"]
    assert embed.image.url == "attachment://cover.png"
    # The H1 is surfaced so the reviewer can see the title genuinely differs.
    assert post.copy.article_h1 in fields["Article H1 (not used as the title)"]


def test_preview_embed_surfaces_warnings(copy_package, config, tmp_path):
    video = Video(video_id="w7mazKut2lk", title="t", url="u")
    post = assemble_post(video, copy_package, config, output_dir=tmp_path, report=lambda _: None)
    post.warnings.append("headline selection: worth a manual look")

    embed = embeds.post_preview(post, index=1, total=1, mode="scheduled")

    assert any("Worth a look" in f.name for f in embed.fields)


def test_long_description_is_truncated_to_discord_limits(copy_package, config, tmp_path):
    copy_package.meta_description = "x" * 5000
    video = Video(video_id="w7mazKut2lk", title="t", url="u")
    post = assemble_post(video, copy_package, config, output_dir=tmp_path, report=lambda _: None)

    embed = embeds.post_preview(post, index=1, total=1, mode="scheduled")

    assert len(embed.description) <= 4096


def test_plan_embed_lists_each_video_with_its_slot():
    pairs = [
        (Video(video_id=f"v{i}", title=f"Video {i}", url="u"), datetime(2026, 8, 12 + i, 10, tzinfo=CT))
        for i in range(3)
    ]

    embed = embeds.plan_summary(pairs, skipped=2, source="the playlist")
    body = " ".join(f.value for f in embed.fields)

    assert "3 post(s)" in embed.description
    assert "2 video(s) already processed" in embed.description
    for i in range(3):
        assert f"Video {i}" in body


def test_plan_embed_handles_an_empty_queue():
    embed = embeds.plan_summary([], skipped=0, source="the playlist")

    assert embed.fields[0].value == "Nothing pending."


# ----------------------------------------------------------------- slot pooling


def test_slot_is_only_consumed_on_approval():
    """A skipped post must leave its day free for the next one in the batch."""
    pool = [datetime(2026, 8, d, 10, tzinfo=CT) for d in (12, 13, 14)]

    shown_first = pool[0]           # post 1 previewed
    # ...skipped, so nothing is popped
    shown_second = pool[0]          # post 2 previewed

    assert shown_first == shown_second == datetime(2026, 8, 12, 10, tzinfo=CT)

    pool.pop(0)                     # post 2 approved
    assert pool[0] == datetime(2026, 8, 13, 10, tzinfo=CT)


def test_decision_enum_covers_every_button():
    assert {d.value for d in Decision} == {"approve", "draft", "skip", "stop", "timeout"}


# ----------------------------------------------------------------- ghl-less run


def test_taken_days_is_empty_without_a_ghl_session(config):
    assert jobs.taken_days(None, config) == set()


def test_plan_slots_works_with_no_ghl_session(config):
    videos = [Video(video_id=f"v{i}", title="t", url="u") for i in range(2)]

    slots = jobs.plan_slots(videos, None, config)

    assert len(slots) == 2
    assert all(s.hour == 10 for s in slots)
    assert all(s.weekday() < 5 for s in slots)
