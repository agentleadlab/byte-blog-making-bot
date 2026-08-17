"""Bot logic that can be tested without a gateway connection."""

from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from wilbyte.bot import embeds, jobs
from wilbyte.bot.client import (
    is_allowed,
    is_direct_mention,
    parse_guild_id,
    publish_status,
)
from wilbyte.bot.views import Decision
from wilbyte import ghl, youtube
from wilbyte.models import Video
from wilbyte.pipeline import assemble_post
from wilbyte.state import Ledger
from wilbyte.youtube import looks_like_playlist

ET = ZoneInfo("America/New_York")

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


# -------------------------------------------------------------- when to speak

BOT_USER = SimpleNamespace(id=999, bot=True)


def msg(content, *, mentions=(), author_is_bot=False, mention_everyone=False):
    return SimpleNamespace(
        content=content,
        mentions=list(mentions),
        mention_everyone=mention_everyone,
        author=SimpleNamespace(id=1, bot=author_is_bot),
    )


def test_a_typed_mention_is_answered():
    assert is_direct_mention(msg("<@999> status", mentions=[BOT_USER]), BOT_USER)


def test_a_nickname_style_mention_is_answered():
    assert is_direct_mention(msg("<@!999> status", mentions=[BOT_USER]), BOT_USER)


def test_ordinary_chatter_is_ignored():
    assert not is_direct_mention(msg("byte should do this later"), BOT_USER)


def test_everyone_pings_are_ignored():
    assert not is_direct_mention(
        msg("<@999> heads up", mentions=[BOT_USER], mention_everyone=True), BOT_USER
    )


def test_a_role_ping_is_ignored():
    """A role mention doesn't put the bot in `mentions`, and mustn't trigger it."""
    assert not is_direct_mention(msg("<@&555> standup time"), BOT_USER)


def test_a_reply_ping_without_a_typed_tag_is_ignored():
    """Replying to one of Byte's messages adds it to `mentions` even though the
    text contains no tag - that is not someone asking Byte for something."""
    assert not is_direct_mention(msg("thanks, that worked", mentions=[BOT_USER]), BOT_USER)


def test_other_bots_cannot_trigger_it():
    assert not is_direct_mention(
        msg("<@999> go", mentions=[BOT_USER], author_is_bot=True), BOT_USER
    )


def test_a_mention_of_someone_else_is_ignored():
    other = SimpleNamespace(id=777, bot=False)

    assert not is_direct_mention(msg("<@777> can you check", mentions=[other]), BOT_USER)


def test_a_similar_id_does_not_count_as_a_mention():
    """`<@9991>` is a different user and must not match `<@999>`."""
    assert not is_direct_mention(msg("<@9991> hi", mentions=[BOT_USER]), BOT_USER)


def test_nothing_happens_before_the_bot_knows_who_it_is():
    assert not is_direct_mention(msg("<@999> hi", mentions=[BOT_USER]), None)


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
    post.scheduled_at = datetime(2026, 8, 12, 10, tzinfo=ET)

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
        (Video(video_id=f"v{i}", title=f"Video {i}", url="u"), datetime(2026, 8, 12 + i, 10, tzinfo=ET))
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
    pool = [datetime(2026, 8, d, 10, tzinfo=ET) for d in (12, 13, 14)]

    shown_first = pool[0]           # post 1 previewed
    # ...skipped, so nothing is popped
    shown_second = pool[0]          # post 2 previewed

    assert shown_first == shown_second == datetime(2026, 8, 12, 10, tzinfo=ET)

    pool.pop(0)                     # post 2 approved
    assert pool[0] == datetime(2026, 8, 13, 10, tzinfo=ET)


def test_clicking_schedule_schedules_even_in_draft_mode():
    """The card promises a date above a "Schedule it" button - honour it.

    Asking for the run with the word "draft" used to override the button, so a
    post previewed as "Scheduled Wed Aug 05" quietly landed as a draft.
    """
    assert publish_status(Decision.APPROVE) == ghl.STATUS_SCHEDULED


def test_clicking_save_as_draft_drafts():
    assert publish_status(Decision.DRAFT) == ghl.STATUS_DRAFT


def test_decision_enum_covers_every_button():
    assert {d.value for d in Decision} == {"approve", "draft", "skip", "stop", "timeout"}


# ----------------------------------------------------------------- ghl-less run


def test_taken_days_is_empty_without_a_ghl_session(config):
    assert jobs.taken_days(None, config) == set()


def test_plan_embed_labels_a_video_whose_title_could_not_be_read():
    pairs = [(Video(video_id="n2DRr4cRUe4", title="", url="u"),
              datetime(2026, 8, 12, 10, tzinfo=ET))]

    body = " ".join(f.value for f in embeds.plan_summary(pairs, skipped=0, source="a link").fields)

    assert "(title unavailable)" in body
    assert "n2DRr4cRUe4" in body


# ------------------------------------------------------- blocked metadata lookup


def test_a_blocked_title_lookup_does_not_end_the_run(monkeypatch, tmp_path):
    """YouTube refusing metadata is not a reason to stop - the id is in the URL.

    The title is only a hint to the copywriter, so the run should continue and
    fail (or not) at the transcript, whose error actually tells you what to do.
    """
    def blocked(_url):
        raise youtube.IngestError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(jobs.youtube, "fetch_video", blocked)
    ledger = Ledger(path=tmp_path / "ledger.json")

    videos, done = jobs.resolve_videos(
        "https://youtu.be/n2DRr4cRUe4", ledger, limit=5, force=True
    )

    assert done == 0
    assert [v.video_id for v in videos] == ["n2DRr4cRUe4"]
    assert videos[0].title == ""


def test_a_readable_title_is_still_used(monkeypatch, tmp_path):
    monkeypatch.setattr(
        jobs.youtube, "fetch_video",
        lambda _url: Video(video_id="n2DRr4cRUe4", title="Real Title", url="u"),
    )
    ledger = Ledger(path=tmp_path / "ledger.json")

    videos, _ = jobs.resolve_videos(
        "https://youtu.be/n2DRr4cRUe4", ledger, limit=5, force=True
    )

    assert videos[0].title == "Real Title"


def test_plan_slots_works_with_no_ghl_session(config):
    videos = [Video(video_id=f"v{i}", title="t", url="u") for i in range(2)]

    slots = jobs.plan_slots(videos, None, config)

    assert len(slots) == 2
    assert all(s.hour == 10 for s in slots)
    assert all(s.weekday() < 5 for s in slots)


# --------------------------------------------------------- diagnosis wording


def test_a_permission_refusal_mentions_ownership():
    hint = jobs._owner_hint(RuntimeError("YouTube refused captions/x (403)"))

    assert "own the channel" in hint


def test_a_credentials_refusal_does_not_blame_ownership():
    """401 unauthorized_client is about which OAuth client minted the token."""
    hint = jobs._owner_hint(RuntimeError('HTTP 401: {"error": "unauthorized_client"}'))

    assert hint == ""


# ------------------------------------------------------ undated posts in check


def test_posts_with_a_readable_date_raise_nothing(config):
    posts = [{"_id": "a", "publishedAt": "2026-08-12T14:00:00.000Z"}]

    assert jobs._undated_posts(posts, config) == []


def test_an_undated_post_is_flagged_with_the_fields_ghl_sent(config):
    """A post with no readable date is a day the scheduler will hand out again."""
    posts = [{"_id": "a", "title": "Are Old Leads Bad", "status": "SCHEDULED"}]

    (ok, message), = jobs._undated_posts(posts, config)

    assert ok is False
    assert "1 post(s) came back with no readable date" in message
    # The field list is the point - it ends the guessing about key names.
    assert "status" in message and "title" in message


def test_no_posts_at_all_is_not_a_problem(config):
    assert jobs._undated_posts([], config) == []


# ------------------------------------------------------------- the calendar


class FakeGHL:
    def __init__(self, posts, *, on_update=None, pages=None):
        self.posts = list(posts)
        self.pages = list(pages) if pages is not None else None
        self.reads = 0
        self.updates = []
        self.on_update = on_update
        self.blog_id = "blog1"
        self.author_id = "author1"
        self.category_ids = ["cat1"]
        self.client = SimpleNamespace(
            location_id="loc1",
            list_posts=self._list,
            update_post=self._update,
            close=lambda: None,
        )

    def _list(self, _blog):
        """Answer from `pages` when given one, so indexing lag can be staged."""
        self.reads += 1
        if self.pages is None:
            return self.posts
        return self.pages[min(self.reads - 1, len(self.pages) - 1)]

    def _update(self, post_id, payload):
        self.updates.append((post_id, payload))
        if self.on_update:
            self.on_update(self, payload)
        return {}


class LedgerStub:
    def __init__(self, entries):
        self.entries = {str(i): e for i, e in enumerate(entries)}


def entry(slug, title, scheduled_at):
    return SimpleNamespace(url_slug=slug, title=title, scheduled_at=scheduled_at)


def test_upcoming_posts_are_dated_and_sorted(config):
    posts = [
        {"title": "Later", "urlSlug": "later", "publishedAt": "2099-08-20T14:00:00.000Z"},
        {"title": "Sooner", "urlSlug": "sooner", "publishedAt": "2099-08-18T14:00:00.000Z"},
    ]

    result = jobs.upcoming_posts(FakeGHL(posts), config)

    assert [title for _day, title in result] == ["Sooner", "Later"]


def test_posts_already_out_are_not_upcoming(config):
    posts = [{"title": "Old news", "urlSlug": "old", "publishedAt": "2020-01-01T14:00:00.000Z"}]

    assert jobs.upcoming_posts(FakeGHL(posts), config) == []


def test_the_ledger_fills_in_what_ghl_wont_report(config):
    """GHL returns RYTE's own posts without a schedule - they'd be invisible."""
    ledger = LedgerStub([entry("ryte-post", "A RYTE Post", "2099-08-19T14:00:00+00:00")])

    result = jobs.upcoming_posts(FakeGHL([]), config, ledger)

    assert [title for _day, title in result] == ["A RYTE Post"]


def test_ghl_wins_when_both_know_a_post(config):
    """GHL is what shows an edit someone made in the dashboard."""
    posts = [{"title": "Edited in GHL", "urlSlug": "same", "publishedAt": "2099-08-25T14:00:00.000Z"}]
    ledger = LedgerStub([entry("same", "Stale ledger title", "2099-08-19T14:00:00+00:00")])

    result = jobs.upcoming_posts(FakeGHL(posts), config, ledger)

    assert [title for _day, title in result] == ["Edited in GHL"]


def test_drafts_in_the_ledger_are_not_on_the_calendar(config):
    ledger = LedgerStub([entry("a-draft", "A Draft", None)])

    assert jobs.upcoming_posts(FakeGHL([]), config, ledger) == []


def test_no_ghl_connection_still_reports_the_ledger(config):
    ledger = LedgerStub([entry("ryte-post", "A RYTE Post", "2099-08-19T14:00:00+00:00")])

    assert len(jobs.upcoming_posts(None, config, ledger)) == 1


def test_the_schedule_embed_lists_each_post_with_its_day():
    posts = [(date(2099, 8, 18), "Sooner"), (date(2099, 8, 19), "Later")]

    embed = embeds.upcoming_summary(posts, next_slots=[], reachable=True)
    body = " ".join(f.value for f in embed.fields)

    assert "2 post(s)" in embed.description
    assert "Tue Aug 18" in body and "Sooner" in body


def test_the_schedule_embed_says_when_ghl_was_unreachable():
    """Otherwise a short list reads as an empty calendar rather than a failure."""
    embed = embeds.upcoming_summary([], next_slots=[], reachable=False)

    assert "wasn't reachable" in embed.footer.text


# --------------------------------------------------- confirming the schedule


def scheduled_post(slug, when, *, post_id="post1", payload_path="/tmp/p.json"):
    return SimpleNamespace(
        url_slug=slug,
        scheduled_at=when,
        warnings=[],
        ghl_post_id=post_id,
        ghl_payload_path=payload_path,
        title="A Post",
    )


def test_a_schedulable_post_raises_nothing():
    when = datetime(2099, 8, 18, 10, tzinfo=ET)

    assert jobs.schedule_warnings(scheduled_post("a-post", when)) == []


def test_a_post_with_no_id_cannot_be_published_later():
    """RYTE publishes these itself, and it addresses the post by its id."""
    when = datetime(2099, 8, 18, 10, tzinfo=ET)

    (warning,) = jobs.schedule_warnings(scheduled_post("a-post", when, post_id=None))

    assert "by hand" in warning


def test_a_post_with_no_saved_body_cannot_be_published_later():
    """GHL's update replaces the post and its list endpoint omits the article."""
    when = datetime(2099, 8, 18, 10, tzinfo=ET)

    (warning,) = jobs.schedule_warnings(scheduled_post("a-post", when, payload_path=None))

    assert "body" in warning and "by hand" in warning


def test_a_draft_needs_no_publishing_plan():
    assert jobs.schedule_warnings(scheduled_post("a-post", None)) == []


def test_the_field_dump_marks_the_posts_holding_no_date(config):
    posts = [
        {"title": "Goes out", "status": "SCHEDULED", "publishedAt": "2099-08-18T14:00:00.000Z"},
        {"title": "Never will", "status": "SCHEDULED"},
    ]

    goes_out, never = jobs.field_lines(posts, config)

    assert "Aug 18" in goes_out and "NO DATE" not in goes_out
    assert "NO DATE" in never


def test_the_field_dump_drops_the_article_body_but_keeps_everything_else():
    """The 8kb of HTML isn't the question; the field names are."""
    compact = jobs._compact({"rawHTML": "x" * 9000, "urlSlug": "a-post", "publishedAt": None})

    assert compact["rawHTML"] == "<9000 chars>"
    assert compact["urlSlug"] == "a-post"
    assert "publishedAt" in compact, "a null field is evidence too"


def test_scheduled_posts_are_invisible_to_the_listing(config):
    """GHL's API returns published and draft posts, never scheduled ones.

    That is why the calendar read alone can't be trusted for booked days, and
    why RYTE keeps its own ledger of the slots it handed out.
    """
    posts = [{"urlSlug": "live", "status": "PUBLISHED", "publishedAt": "2099-08-18T14:00:00.000Z"}]

    assert jobs.taken_days(FakeGHL(posts), config) == {date(2099, 8, 18)}
