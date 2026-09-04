"""Bot logic that can be tested without a gateway connection."""
import asyncio

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from wilbyte.bot import embeds, jobs
from wilbyte.bot.responders import ChannelResponder
from wilbyte.bot.client import (
    is_allowed,
    is_direct_mention,
    parse_guild_id,
    publish_status,
)
from wilbyte.bot.views import Decision
from wilbyte import ghl, publisher, youtube
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


def entry(slug, title, scheduled_at, published_at=None):
    return SimpleNamespace(
        url_slug=slug, title=title, scheduled_at=scheduled_at, published_at=published_at
    )


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


# --------------------------------------------------- reconciling with GHL


class SlugGHL(FakeGHL):
    """A GHL where only some slugs still exist."""

    def __init__(self, existing, *, broken=()):
        super().__init__([])
        self.existing = set(existing)
        self.broken = set(broken)
        self.client.slug_exists = self._exists

    def _exists(self, slug, **_kwargs):
        if slug in self.broken:
            raise ghl.GHLError("HTTP 500")
        return slug in self.existing


def ledger_of(tmp_path, *entries):
    ledger = Ledger(path=tmp_path / "ledger.json")
    for video_id, slug, scheduled, published in entries:
        ledger.record(
            video_id=video_id, title=slug, url_slug=slug,
            scheduled_at=scheduled, ghl_post_id="p" + video_id,
            payload_path=str(tmp_path / f"{slug}.json"),
            published_at=published,
        )
    return ledger


def test_a_deleted_post_stops_holding_its_day(tmp_path):
    """Delete a post in GHL and RYTE would hold that day forever otherwise."""
    when = datetime(2099, 8, 20, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "gone", when, None), ("v2", "still-here", when, None))

    dropped, kept, problems = jobs.reconcile(SlugGHL({"still-here"}), ledger)

    assert [e.url_slug for e in dropped] == ["gone"]
    assert [e.url_slug for e in kept] == ["still-here"]
    assert problems == []
    assert "v1" not in ledger.entries


def test_a_dropped_entry_lets_the_video_be_redone(tmp_path):
    when = datetime(2099, 8, 20, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "gone", when, None))

    jobs.reconcile(SlugGHL(set()), ledger)

    assert not ledger.has("v1")


def test_posts_already_published_are_left_alone(tmp_path):
    """They are the record of what's been done, not a claim on a day."""
    when = datetime(2020, 1, 1, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "old-news", when, when))

    dropped, kept, _ = jobs.reconcile(SlugGHL(set()), ledger)

    assert dropped == []
    assert [e.url_slug for e in kept] == ["old-news"]


def test_a_failed_check_keeps_the_entry_rather_than_guessing(tmp_path):
    """Throwing away a booked day on a 500 would double-book it."""
    when = datetime(2099, 8, 20, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "unknown", when, None))

    dropped, kept, problems = jobs.reconcile(SlugGHL(set(), broken={"unknown"}), ledger)

    assert dropped == []
    assert [e.url_slug for e in kept] == ["unknown"]
    assert "HTTP 500" in problems[0]


def test_the_drop_survives_a_restart(tmp_path):
    when = datetime(2099, 8, 20, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "gone", when, None))

    jobs.reconcile(SlugGHL(set()), ledger)

    assert "v1" not in Ledger.load(tmp_path / "ledger.json").entries


def test_the_soonest_pending_post_is_the_one_tested(tmp_path):
    ledger = ledger_of(
        tmp_path,
        ("v2", "later", datetime(2099, 8, 20, 10, tzinfo=ET), None),
        ("v1", "sooner", datetime(2099, 8, 18, 10, tzinfo=ET), None),
    )

    assert jobs.next_pending(ledger).url_slug == "sooner"


def test_a_published_post_is_not_offered_for_testing(tmp_path):
    when = datetime(2020, 1, 1, 10, tzinfo=ET)
    ledger = ledger_of(tmp_path, ("v1", "done", when, when))

    assert jobs.next_pending(ledger) is None


def test_changing_status_keeps_the_body_and_the_date(tmp_path):
    """A PUT is a replace - dropping either would blank or misdate the post."""
    import json

    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"rawHTML": "<h1>A</h1>", "status": "SCHEDULED"}))
    ledger = Ledger(path=tmp_path / "ledger.json")
    entry = ledger.record(
        video_id="v1", title="A", url_slug="a", ghl_post_id="post1",
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET), payload_path=str(payload),
    )
    context = FakeGHL([])

    jobs.set_status(context, entry, ghl.STATUS_PUBLISHED)

    _post_id, body = context.updates[0]
    assert body["status"] == "PUBLISHED"
    assert body["rawHTML"] == "<h1>A</h1>"
    assert body["publishedAt"] == "2099-08-18T14:00:00.000Z"


def test_a_post_with_no_ghl_id_is_named_rather_than_skipped(tmp_path):
    """Skipping it quietly is exactly GHL's failure: the day just passes."""
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Orphan", url_slug="orphan", ghl_post_id=None,
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET), payload_path="/tmp/p.json",
    )

    assert jobs.next_pending(ledger) is None
    assert jobs.stuck_posts(ledger) == [("Orphan", "GHL never gave me its post id")]


def test_a_post_with_no_saved_body_is_named_too(tmp_path):
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Bodyless", url_slug="b", ghl_post_id="post1",
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET), payload_path=None,
    )

    (_title, why), = jobs.stuck_posts(ledger)
    assert "body" in why


def test_a_healthy_post_is_not_reported_as_stuck(tmp_path):
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Fine", url_slug="f", ghl_post_id="post1",
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET), payload_path="/tmp/p.json",
    )

    assert jobs.stuck_posts(ledger) == []
    assert jobs.next_pending(ledger).title == "Fine"


def test_a_post_already_published_is_not_listed_as_still_to_go_out(config, tmp_path):
    """It still holds its day, but the list and the count must agree."""
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Already out", url_slug="out",
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET),
        ghl_post_id="p1", payload_path="/tmp/p.json",
        published_at=datetime(2099, 8, 18, 10, tzinfo=ET),
    )
    ledger.record(
        video_id="v2", title="Still waiting", url_slug="waiting",
        scheduled_at=datetime(2099, 8, 19, 10, tzinfo=ET),
        ghl_post_id="p2", payload_path="/tmp/p.json",
    )

    listed = jobs.upcoming_posts(None, config, ledger)

    assert [title for _day, title in listed] == ["Still waiting"]


def test_a_published_post_still_holds_its_day(config, tmp_path):
    """Freeing it would book a second post onto a day that already has one."""
    when = datetime(2099, 8, 18, 10, tzinfo=ET)
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Already out", url_slug="out", scheduled_at=when,
        ghl_post_id="p1", payload_path="/tmp/p.json", published_at=when,
    )

    assert date(2099, 8, 18) in jobs.taken_days(None, config, ledger)


# --------------------------------------------------- several links in one message


class FakeYT:
    """Stands in for YouTube so link expansion can be tested offline."""

    def __init__(self):
        self.asked = []

    def video_from_link(self, url):
        self.asked.append(url)
        return Video(video_id=url.rsplit("/", 1)[-1], title=url, url=url)


def resolve(sources, ledger, monkeypatch, *, limit=10, force=True):
    fake = FakeYT()
    monkeypatch.setattr(jobs.youtube, "video_from_link", fake.video_from_link)
    monkeypatch.setattr(jobs.youtube, "looks_like_playlist", lambda _s: False)
    videos, done = jobs.resolve_many(
        sources, ledger, limit=limit, force=force, offline=True
    )
    return videos, done, fake


def test_every_link_becomes_its_own_video(tmp_path, monkeypatch):
    ledger = Ledger(path=tmp_path / "ledger.json")
    links = ["https://youtu.be/aaa", "https://youtu.be/bbb", "https://youtu.be/ccc"]

    videos, _done, _fake = resolve(links, ledger, monkeypatch)

    assert [v.video_id for v in videos] == ["aaa", "bbb", "ccc"]


def test_the_order_they_were_typed_is_kept(tmp_path, monkeypatch):
    """Slots are handed out in order, so this decides which post lands when."""
    ledger = Ledger(path=tmp_path / "ledger.json")
    links = ["https://youtu.be/ccc", "https://youtu.be/aaa"]

    videos, _done, _fake = resolve(links, ledger, monkeypatch)

    assert [v.video_id for v in videos] == ["ccc", "aaa"]


def test_a_video_named_twice_makes_one_post(tmp_path, monkeypatch):
    ledger = Ledger(path=tmp_path / "ledger.json")
    links = ["https://youtu.be/aaa", "https://youtu.be/bbb", "https://youtu.be/aaa"]

    videos, _done, _fake = resolve(links, ledger, monkeypatch)

    assert [v.video_id for v in videos] == ["aaa", "bbb"]


def test_the_limit_stops_the_expansion_early(tmp_path, monkeypatch):
    """Twenty links shouldn't cost twenty YouTube lookups to build ten posts."""
    ledger = Ledger(path=tmp_path / "ledger.json")
    links = [f"https://youtu.be/v{i}" for i in range(20)]

    videos, _done, fake = resolve(links, ledger, monkeypatch, limit=3)

    assert len(videos) == 3
    assert len(fake.asked) == 3


def test_links_already_posted_are_counted_not_rebuilt(tmp_path, monkeypatch):
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="aaa", title="Done", url_slug="done",
        scheduled_at=None, ghl_post_id="p1",
    )
    links = ["https://youtu.be/aaa", "https://youtu.be/bbb"]

    videos, done, _fake = resolve(links, ledger, monkeypatch, force=False)

    assert [v.video_id for v in videos] == ["bbb"]
    assert done == 1


# ------------------------------------------------- offering the leftovers back


class Recorder:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, file=None, view=None):
        self.messages.append(content or "")


def run_offer(videos):
    import asyncio

    from wilbyte.bot.client import _offer_retry

    recorder = Recorder()
    asyncio.run(_offer_retry(recorder, videos))
    return recorder.messages


def vid(video_id):
    return Video(video_id=video_id, title=video_id, url=f"https://youtu.be/{video_id}")


def test_the_leftovers_come_back_as_a_command_to_paste():
    """"Skipped 4" says what happened and nothing about what to do next."""
    (message,) = run_offer([vid("aaa"), vid("bbb")])

    assert "2 didn't get posted" in message
    assert "@RYTE https://youtu.be/aaa https://youtu.be/bbb force" in message


def test_a_clean_run_offers_nothing():
    assert run_offer([]) == []


def test_the_same_video_is_only_offered_once():
    (message,) = run_offer([vid("aaa"), vid("aaa")])

    assert message.count("youtu.be/aaa") == 1


# --------------------------------------------- finding what was written but not posted


def built_post(out_dir, slug, title, video_id):
    """Mimic what a build writes before the approval prompt goes up."""
    post_dir = out_dir / slug
    post_dir.mkdir(parents=True)
    (post_dir / "ghl-fields.txt").write_text(
        f"Title:            {title}\n"
        f"URL slug:         {slug}\n"
        f"Source video:     https://youtu.be/{video_id}\n",
        encoding="utf-8",
    )


def test_a_post_that_timed_out_is_found_with_its_link(tmp_path):
    """The ledger never hears about a skipped post; the files on disk do."""
    out = tmp_path / "out"
    built_post(out, "no-answer", "The Niche Lead Filter Every Agent Should Use", "abc123")
    ledger = Ledger(path=tmp_path / "ledger.json")

    assert jobs.built_but_not_posted(out, ledger) == [
        ("The Niche Lead Filter Every Agent Should Use", "https://youtu.be/abc123")
    ]


def test_a_post_that_reached_ghl_is_not_offered_again(tmp_path):
    out = tmp_path / "out"
    built_post(out, "posted", "Already Live", "abc123")
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="abc123", title="Already Live", url_slug="posted",
        scheduled_at=None, ghl_post_id="p1",
    )

    assert jobs.built_but_not_posted(out, ledger) == []


def test_a_renamed_slug_still_counts_as_posted(tmp_path):
    """Publishing can add -2 to avoid a clash, so folder names stop matching."""
    out = tmp_path / "out"
    built_post(out, "clashing-slug", "Already Live", "abc123")
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="abc123", title="Already Live", url_slug="clashing-slug-2",
        scheduled_at=None, ghl_post_id="p1",
    )

    assert jobs.built_but_not_posted(out, ledger) == []


def test_no_output_directory_is_not_an_error(tmp_path):
    assert jobs.built_but_not_posted(tmp_path / "nothing-here", Ledger(path=tmp_path / "l.json")) == []


# ------------------------------------------------- watching an announcements channel


def announcement(channel_id, *, content="", embeds=(), author_is_bot=True):
    return SimpleNamespace(
        content=content,
        embeds=list(embeds),
        mentions=[],
        mention_everyone=False,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=555, bot=author_is_bot),
    )


def embed(*, url=None, title=None, description=None):
    return SimpleNamespace(url=url, title=title, description=description, author=None)


def watching(config, *channel_ids):
    secrets = replace(config.secrets, discord_watch_channel_ids=tuple(channel_ids))
    return replace(config, secrets=secrets)


def test_a_watched_channel_is_acted_on(config):
    from wilbyte.bot.client import is_watched

    assert is_watched(announcement("111"), watching(config, "111"))


def test_other_channels_are_left_alone(config):
    from wilbyte.bot.client import is_watched

    assert not is_watched(announcement("222"), watching(config, "111"))


def test_nothing_is_watched_by_default(config):
    from wilbyte.bot.client import is_watched

    assert not is_watched(announcement("111"), config)


def test_a_bot_posting_the_announcement_still_counts(config):
    """The whole point: the video is announced by another bot, and
    is_direct_mention refuses bots on purpose."""
    from wilbyte.bot.client import is_direct_mention, is_watched

    message = announcement("111", content="New video https://youtu.be/abc123")

    assert not is_direct_mention(message, BOT_USER)
    assert is_watched(message, watching(config, "111"))


def test_the_link_is_found_in_the_message_text():
    from wilbyte.bot.client import watched_links

    message = announcement("111", content="🎬 New video just dropped! https://youtu.be/abc123")

    assert watched_links(message) == ("https://youtu.be/abc123",)


def test_the_link_is_found_in_the_embed_discord_unfurled():
    """An announcement bot posts a line of text and lets Discord unfurl it, so
    the link is only in the embed."""
    from wilbyte.bot.client import watched_links

    message = announcement(
        "111",
        content="🎬 New video just dropped! @everyone — check it out 👇",
        embeds=[embed(url="https://www.youtube.com/watch?v=abc123", title="How To Stop Blaming")],
    )

    assert watched_links(message) == ("https://www.youtube.com/watch?v=abc123",)


def test_a_link_in_the_embed_description_is_found():
    from wilbyte.bot.client import watched_links

    message = announcement(
        "111", embeds=[embed(description="Watch: https://youtu.be/abc123 for the full breakdown")]
    )

    assert watched_links(message) == ("https://youtu.be/abc123",)


def test_the_same_link_in_text_and_embed_is_one_post():
    from wilbyte.bot.client import watched_links

    message = announcement(
        "111",
        content="New video https://youtu.be/abc123",
        embeds=[embed(url="https://youtu.be/abc123")],
    )

    assert watched_links(message) == ("https://youtu.be/abc123",)


def test_chatter_with_no_link_starts_nothing():
    from wilbyte.bot.client import watched_links

    assert watched_links(announcement("111", content="great episode today")) == ()


def test_a_non_youtube_link_is_ignored():
    from wilbyte.bot.client import watched_links

    message = announcement("111", content="see https://agentleadlab.com/blog for more")

    assert watched_links(message) == ()


def test_anyone_can_approve_a_post_nobody_asked_for():
    """Locking the buttons to whoever announced it would lock them to a bot."""
    from wilbyte.bot.views import ApprovalView

    view = ApprovalView(requester_id=None, timeout=1)

    assert view.requester_id is None


def test_ryte_stays_silent_when_mentioned_in_a_watched_channel(config):
    """It is a guest in that server: there to see new videos, nothing else."""
    import asyncio

    from wilbyte.bot.client import handle_mention

    replies = []
    message = SimpleNamespace(
        content="<@999> what's the status?",
        embeds=[],
        mentions=[BOT_USER],
        mention_everyone=False,
        channel=SimpleNamespace(id="111"),
        author=SimpleNamespace(id=7, bot=False),
        reply=lambda *a, **k: replies.append(a),
    )
    bot = SimpleNamespace(config=watching(config, "111"), user=BOT_USER, run_lock=None)

    asyncio.run(handle_mention(bot, message))

    assert replies == []


def test_a_blocked_channel_gets_no_public_refusal(config):
    """"RYTE isn't enabled here" is still chatter, in front of clients."""
    import asyncio

    from wilbyte.bot.client import handle_mention

    replies = []
    secrets = replace(config.secrets, discord_channel_ids=("999",))
    message = SimpleNamespace(
        content="<@999> status",
        embeds=[],
        mentions=[BOT_USER],
        mention_everyone=False,
        channel=SimpleNamespace(id="222"),
        author=SimpleNamespace(id=7, bot=False, roles=[]),
        reply=lambda *a, **k: replies.append(a),
    )
    bot = SimpleNamespace(config=replace(config, secrets=secrets), user=BOT_USER, run_lock=None)

    asyncio.run(handle_mention(bot, message))

    assert replies == []


# ------------------------------------------------- restarting onto a new version


def test_the_restart_code_is_the_one_the_launcher_watches_for():
    """scripts/start.sh loops on exactly this and nothing else."""
    from pathlib import Path

    from wilbyte.bot.client import RESTART_EXIT_CODE

    launcher = Path(__file__).resolve().parents[1] / "scripts" / "start.sh"

    assert f"RESTART_CODE={RESTART_EXIT_CODE}" in launcher.read_text()


def test_the_launcher_does_not_loop_on_a_crash():
    """Restarting forever on a broken build is worse than staying down."""
    from pathlib import Path

    launcher = (Path(__file__).resolve().parents[1] / "scripts" / "start.sh").read_text()

    assert 'if [ "$code" -ne "$RESTART_CODE" ]; then' in launcher
    assert 'exit "$code"' in launcher


# --------------------------------------------- artwork taken from the page


def test_a_notion_uploaded_image_is_found():
    assert jobs._asset_url({"type": "file", "file": {"url": "https://s3/x.png"}}) == (
        "https://s3/x.png"
    )


def test_an_external_image_is_found():
    assert jobs._asset_url({"type": "external", "external": {"url": "https://x/y.png"}}) == (
        "https://x/y.png"
    )


def test_an_emoji_icon_has_no_url_to_copy():
    """A page wearing an emoji has no image, and that is not an error."""
    assert jobs._asset_url({"type": "emoji", "emoji": "🎙️"}) == ""


def test_a_page_with_no_cover_is_not_an_error():
    assert jobs._asset_url(None) == ""


# ------------------------------------- naming a call in the message itself

# No menu and no slash command. A name typed beside the link settles which
# recording it is, and a name typed on its own answers RYTE when it asks.


def test_a_name_beside_the_link_is_kept():
    from wilbyte.bot.client import _words_beside_link

    text = "<@123> https://us06web.zoom.us/rec/share/abc.def derrick"

    assert _words_beside_link(text, "https://us06web.zoom.us/rec/share/abc.def") == "derrick"


def test_the_passcode_line_is_not_mistaken_for_a_name():
    from wilbyte.bot.client import _words_beside_link

    text = "https://us06web.zoom.us/rec/share/abc.def\nPasscode: U^M^s7Bw"

    assert _words_beside_link(text, "https://us06web.zoom.us/rec/share/abc.def") == ""


def test_the_command_word_is_not_hunted_for_as_a_name():
    from wilbyte.bot.client import _words_beside_link

    text = "<@123> recording https://fathom.video/calls/1 arnold"

    assert _words_beside_link(text, "https://fathom.video/calls/1") == "arnold"


def test_a_link_with_nothing_beside_it_names_nobody():
    from wilbyte.bot.client import _words_beside_link

    assert _words_beside_link("https://fathom.video/calls/1", "https://fathom.video/calls/1") == ""


def test_a_bare_name_is_read_as_an_answer():
    """"derrick" on its own must not come back as the help text."""
    from wilbyte.bot.client import _words_beside_link

    assert _words_beside_link("<@123> derrick robison", "") == "derrick robison"


def test_zooms_marketing_title_is_not_a_recording_name():
    """It was served for every gated link, so it matched every link posted."""
    from wilbyte import zoom

    html = (
        '<meta property="og:title" content="Video Conferencing, Web Conferencing, '
        'Webinars, Screen Sharing">'
    )

    assert zoom.topic_from_page(html) == ""


def test_a_search_narrows_what_the_picker_shows():
    """Typing a name beside the link should shorten the list, not replace it."""
    from wilbyte.bot import jobs

    calls = [
        jobs.Call("zoom", "a", "Derrick Robison", "2026-08-19T05:17:00Z", "santi@x.com"),
        jobs.Call("zoom", "b", "Arlene Linares", "2026-08-21T10:00:00Z", "santi@x.com"),
    ]

    assert [c.topic for c in calls if c.matches("derrick")] == ["Derrick Robison"]
    assert len([c for c in calls if c.matches("")]) == 2


# --------------------------- an optional setting must not take RYTE down

# A server id RYTE has no access to raised 403 out of setup_hook and killed the
# login before the bot ever connected. Slash commands are a convenience;
# @mentions are the way in that matters.


class _Tree:
    def __init__(self, fails_on_guild=False, fails_always=False):
        self.fails_on_guild = fails_on_guild
        self.fails_always = fails_always
        self.synced = []

    def copy_global_to(self, *, guild):
        pass

    async def sync(self, *, guild=None):
        import discord

        if self.fails_always or (guild is not None and self.fails_on_guild):
            raise discord.HTTPException(_Response(), "50001: Missing Access")
        self.synced.append(guild)


class _Response:
    status = 403
    reason = "Forbidden"


async def _run_setup(tree, guild_id):
    from types import SimpleNamespace

    from wilbyte.bot.client import WilByteBot

    bot = WilByteBot.__new__(WilByteBot)
    bot.tree = tree
    bot.config = SimpleNamespace(secrets=SimpleNamespace(discord_guild_id=guild_id))
    import wilbyte.bot.client as client_mod

    original = client_mod.register_commands
    client_mod.register_commands = lambda _bot: None
    try:
        await WilByteBot.setup_hook(bot)
    finally:
        client_mod.register_commands = original


def test_a_server_ryte_cannot_reach_falls_back_to_a_global_sync():
    tree = _Tree(fails_on_guild=True)

    asyncio.run(_run_setup(tree, "123456789012345678"))

    assert tree.synced == [None], "the guild sync failed, the global one ran"


def test_commands_failing_entirely_still_lets_the_bot_start():
    tree = _Tree(fails_always=True)

    # The point is that this returns rather than raising.
    asyncio.run(_run_setup(tree, "123456789012345678"))

    assert tree.synced == []


def test_the_sop_channel_is_allowed_without_being_listed_twice():
    """RYTE files what lands in the SOP channel, then couldn't answer a question
    in it - the channel allowlist didn't know about it."""
    from types import SimpleNamespace

    from wilbyte.bot.client import is_allowed

    config = SimpleNamespace(secrets=SimpleNamespace(
        discord_channel_ids=("111",), discord_sop_channel_ids=("222",), discord_role_ids=(),
    ))

    assert is_allowed(channel_id=222, user=None, config=config)[0] is True
    assert is_allowed(channel_id=111, user=None, config=config)[0] is True
    assert is_allowed(channel_id=333, user=None, config=config)[0] is False


# --------------------------- catching up on what was posted while off

# The Mac gets turned off at the end of the day. Things get posted over a
# weekend. Remembering to say `backfill` on Monday is the kind of step this was
# built to remove.


class _Msg:
    def __init__(self, id, content, *, bot=False):
        from datetime import datetime
        self.id = id
        self.content = content
        self.author = SimpleNamespace(display_name="K2", bot=bot)
        self.created_at = datetime(2026, 8, 22, 9, 0)
        self.attachments = []

    async def add_reaction(self, emoji):
        pass


class _Channel:
    def __init__(self, messages):
        self.messages = messages
        self.mention = "#sop"

    def history(self, *, limit, oldest_first):
        async def walk():
            for message in (self.messages if oldest_first else reversed(self.messages)):
                yield message
        return walk()


def _run_history(bot, channel):
    from wilbyte.bot.client import _file_channel_history

    return asyncio.run(_file_channel_history(bot, channel, counted=True))


def _point_at(monkeypatch, sops, store):
    """Send the filed-message store at a temp file, without recursing into the
    replacements - the originals have to be bound before they are replaced."""
    was_filed, was_remember = sops.already_filed, sops.remember
    monkeypatch.setattr(sops, "already_filed", lambda mid, path=None: was_filed(mid, path=store))
    monkeypatch.setattr(sops, "remember", lambda mid, path=None: was_remember(mid, path=store))


def test_only_what_was_missed_is_filed(monkeypatch, tmp_path):
    """A weekend off, then a catch-up: Saturday and Sunday, nothing else."""
    from wilbyte import sops
    from wilbyte.bot import client as client_mod
    from wilbyte.bot import jobs

    store = tmp_path / "filed.json"
    _point_at(monkeypatch, sops, store)
    monkeypatch.setattr(jobs, "sop_summary", lambda config, sop: "a summary")
    monkeypatch.setattr(jobs, "file_sop", lambda config, sop, summary="": (sop.title, "https://n/x"))

    LOOM = "https://www.loom.com/share/abc"
    friday = _Msg(1, f"**Friday thing**\n{LOOM}")
    saturday = _Msg(2, f"**Saturday thing**\n{LOOM}")
    sunday = _Msg(3, f"**Sunday thing**\n{LOOM}")

    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace()))

    # Friday was filed while RYTE was still on.
    sops.remember(1, path=store)

    filed, _, _, _ = _run_history(bot, _Channel([friday, saturday, sunday]))

    assert filed == ["Saturday thing", "Sunday thing"]


def test_running_the_catch_up_twice_files_nothing_the_second_time(monkeypatch, tmp_path):
    """It runs on every start-up, so it has to cost nothing when nothing changed."""
    from wilbyte import sops
    from wilbyte.bot import jobs

    store = tmp_path / "filed.json"
    _point_at(monkeypatch, sops, store)
    monkeypatch.setattr(jobs, "sop_summary", lambda config, sop: "a summary")
    monkeypatch.setattr(jobs, "file_sop", lambda config, sop, summary="": (sop.title, "https://n/x"))

    channel = _Channel([_Msg(7, "**A thing**\nhttps://www.loom.com/share/abc")])
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace()))

    assert len(_run_history(bot, channel)[0]) == 1
    assert _run_history(bot, channel)[0] == []


def test_the_bots_own_posts_are_never_filed(monkeypatch, tmp_path):
    from wilbyte import sops
    from wilbyte.bot import jobs

    store = tmp_path / "filed.json"
    _point_at(monkeypatch, sops, store)
    monkeypatch.setattr(jobs, "file_sop", lambda config, sop, summary="": (sop.title, "u"))

    channel = _Channel([_Msg(9, "**Mine**\nhttps://loom.com/share/x", bot=True)])
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace()))

    assert _run_history(bot, channel)[0] == []


# ----------------------------------- the "Watching ..." line under his name


def test_no_status_line_by_default(monkeypatch):
    """A status line that has stopped being true is worse than none. His said
    "Watching YouTube so you don't have to" long after he also started filing
    sales calls and keeping the SOP library."""
    from wilbyte.bot.client import _activity

    monkeypatch.delenv("DISCORD_ACTIVITY", raising=False)

    assert _activity() is None


def test_a_plain_line_is_something_he_is_watching(monkeypatch):
    import discord

    from wilbyte.bot.client import _activity

    monkeypatch.setenv("DISCORD_ACTIVITY", "the SOP channel")
    found = _activity()

    assert found.type is discord.ActivityType.watching
    assert found.name == "the SOP channel"


@pytest.mark.parametrize(
    "verb,expected",
    [("watching", "watching"), ("playing", "playing"), ("listening", "listening"),
     ("competing", "competing")],
)
def test_the_verb_can_be_chosen(monkeypatch, verb, expected):
    import discord

    from wilbyte.bot.client import _activity

    monkeypatch.setenv("DISCORD_ACTIVITY", f"{verb}: sales calls")

    assert _activity().type is getattr(discord.ActivityType, expected)
    assert _activity().name == "sales calls"


def test_a_colon_in_an_ordinary_line_is_not_a_verb(monkeypatch):
    """"Watching: the thing" reads fine; "SOP: lead forms" is not a verb."""
    from wilbyte.bot.client import _activity

    monkeypatch.setenv("DISCORD_ACTIVITY", "SOP: lead forms")

    assert _activity().name == "SOP: lead forms"


def test_a_custom_status_carries_no_verb(monkeypatch):
    """"busy being cute" reads as itself. "Playing busy being cute" doesn't."""
    import discord

    from wilbyte.bot.client import _activity

    monkeypatch.setenv("DISCORD_ACTIVITY", "custom: busy being cute 🐹")
    found = _activity()

    assert isinstance(found, discord.CustomActivity)
    assert found.name == "busy being cute 🐹"


# ------------------------------- a message too long to send is not sent

# The help text grew past Discord's 2000-character limit as commands were added
# to it. Discord refuses the whole message, the exception is not one the
# pipeline catches, and RYTE went silent on somebody mid-question.


def test_a_short_message_is_sent_whole():
    from wilbyte.bot.responders import split_message

    assert split_message("hello") == ["hello"]


def test_a_long_message_comes_apart_between_its_lines():
    from wilbyte.bot.responders import MAX_MESSAGE, split_message

    lines = "\n".join(f"· entry number {n}" for n in range(300))
    pieces = split_message(lines)

    assert len(pieces) > 1
    assert all(len(piece) <= MAX_MESSAGE for piece in pieces)
    assert all(not piece.startswith("ntry") for piece in pieces), "cut mid-word"


def test_nothing_is_lost_in_the_splitting():
    from wilbyte.bot.responders import split_message

    lines = "\n".join(f"line {n}" for n in range(400))

    assert "\n".join(split_message(lines)) == lines


def test_one_enormous_line_is_cut_rather_than_refused():
    """A very long URL still has to go out - refusing it entirely is worse."""
    from wilbyte.bot.responders import MAX_MESSAGE, split_message

    pieces = split_message("x" * 5000)

    assert len(pieces) == 3
    assert all(len(piece) <= MAX_MESSAGE for piece in pieces)


def test_the_help_text_now_fits():
    """This is the message that went silent."""
    from wilbyte.bot import mentions
    from wilbyte.bot.responders import MAX_MESSAGE, split_message

    body = f"{mentions.HELP_TEXT}\n\n-# Running abc1234"

    assert len(body) > MAX_MESSAGE, "still worth splitting"
    assert all(len(piece) <= MAX_MESSAGE for piece in split_message(body))


# ------------------------------------------- moving posts that are already booked

# Opening the weekend up doesn't move anything on its own: the posts are still
# sitting on the weekdays they were given. These cover the part that writes.


def booked(tmp_path, *rows):
    """A ledger of scheduled posts, each with a real payload on disk."""
    import json

    ledger = Ledger(path=tmp_path / "ledger.json")
    for video_id, title, when in rows:
        payload = tmp_path / f"{video_id}.json"
        payload.write_text(json.dumps({"rawHTML": f"<h1>{title}</h1>", "status": "SCHEDULED"}))
        ledger.record(
            video_id=video_id, title=title, url_slug=video_id,
            ghl_post_id=f"post-{video_id}", scheduled_at=when, payload_path=str(payload),
        )
    return ledger


def test_pending_posts_are_soonest_first(tmp_path, config):
    ledger = booked(
        tmp_path,
        ("v2", "Second", datetime(2099, 8, 25, 10, tzinfo=ET)),
        ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)),
    )

    assert [title for _, title, _ in jobs.pending_posts(config, ledger)] == ["First", "Second"]


def test_a_post_with_no_saved_body_is_never_offered_for_moving(tmp_path, config):
    """An update to GHL is a replace, so moving it would empty the article."""
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="v1", title="Bodyless", url_slug="b", ghl_post_id="post1",
        scheduled_at=datetime(2099, 8, 24, 10, tzinfo=ET), payload_path=None,
    )

    assert jobs.pending_posts(config, ledger) == []


def test_moving_a_post_keeps_its_body_and_writes_the_new_date(tmp_path, config):
    from wilbyte.rearrange import Move

    ledger = booked(tmp_path, ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)))
    context = FakeGHL([])
    move = Move(
        video_id="v1", title="First",
        was=datetime(2099, 8, 24, 10, tzinfo=ET),
        now=datetime(2099, 8, 22, 10, tzinfo=ET),
    )

    assert jobs.apply_moves(config, ledger, context, [move]) == []

    post_id, body = context.updates[0]
    assert post_id == "post-v1"
    assert body["rawHTML"] == "<h1>First</h1>"
    assert body["status"] == "SCHEDULED"
    assert body["publishedAt"] == "2099-08-22T14:00:00.000Z"
    assert ledger.entries["v1"].scheduled_at.startswith("2099-08-22")


def test_a_post_that_is_not_moving_is_not_re_sent(tmp_path, config):
    """Every write is a chance to break a live post. Don't make pointless ones."""
    from wilbyte.rearrange import Move

    ledger = booked(tmp_path, ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)))
    when = datetime(2099, 8, 24, 10, tzinfo=ET)
    context = FakeGHL([])

    jobs.apply_moves(config, ledger, context, [Move("v1", "First", when, when)])

    assert context.updates == []


def test_one_post_failing_does_not_stop_the_others(tmp_path, config):
    """And the ones that worked are saved, so RYTE and GHL still agree."""
    from wilbyte.rearrange import Move

    ledger = booked(
        tmp_path,
        ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)),
        ("v2", "Second", datetime(2099, 8, 25, 10, tzinfo=ET)),
    )
    context = FakeGHL([])

    def explode(_self, payload):
        if "First" in payload.get("rawHTML", ""):
            raise ghl.GHLError("nope")

    context.on_update = explode
    moves = [
        Move("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET),
             datetime(2099, 8, 22, 10, tzinfo=ET)),
        Move("v2", "Second", datetime(2099, 8, 25, 10, tzinfo=ET),
             datetime(2099, 8, 23, 10, tzinfo=ET)),
    ]

    problems = jobs.apply_moves(config, ledger, context, moves)

    assert len(problems) == 1 and "First" in problems[0]
    assert ledger.entries["v2"].scheduled_at.startswith("2099-08-23")
    assert ledger.entries["v1"].scheduled_at.startswith("2099-08-24"), "not moved, not recorded"


def test_publishing_now_sends_today_as_the_date(tmp_path, config):
    """A PUBLISHED post still carrying Monday's date reads as published on
    Monday everywhere it is listed, which is not what happened."""
    ledger = booked(tmp_path, ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)))
    context = FakeGHL([])

    when = jobs.publish_now(config, ledger, context, ledger.entries["v1"])

    _post_id, body = context.updates[0]
    assert body["status"] == "PUBLISHED"
    assert body["rawHTML"] == "<h1>First</h1>"
    assert body["publishedAt"] == ghl.to_api_timestamp(when)


def test_a_published_post_is_recorded_so_it_never_goes_out_twice(tmp_path, config):
    ledger = booked(tmp_path, ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)))

    jobs.publish_now(config, ledger, FakeGHL([]), ledger.entries["v1"])

    assert ledger.entries["v1"].published_at
    assert jobs.pending_posts(config, ledger) == []


def test_the_post_held_for_a_day_can_be_found(tmp_path, config):
    ledger = booked(
        tmp_path,
        ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)),
        ("v2", "Second", datetime(2099, 8, 25, 10, tzinfo=ET)),
    )

    found = jobs.held_on(config, ledger, date(2099, 8, 25))

    assert [e.title for e in found] == ["Second"]


def test_a_day_holding_two_posts_says_so_rather_than_picking_one(tmp_path, config):
    ledger = booked(
        tmp_path,
        ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)),
        ("v2", "Also", datetime(2099, 8, 24, 15, tzinfo=ET)),
    )

    assert len(jobs.held_on(config, ledger, date(2099, 8, 24))) == 2


def test_an_empty_day_holds_nothing(tmp_path, config):
    ledger = booked(tmp_path, ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)))

    assert jobs.held_on(config, ledger, date(2099, 8, 26)) == []


def test_the_plan_frees_the_days_the_movers_are_sitting_on(tmp_path, config):
    """Otherwise every post blocks its own move - the earliest day is taken, by
    the post already on it - and a whole queue shuffles one day later for no
    reason at all."""
    from wilbyte.scheduler import next_open_slots

    weekend = replace(config, schedule=replace(config.schedule, weekdays_only=False))
    soonest = next_open_slots(set(), 1, weekend.schedule)[0]
    ledger = booked(tmp_path, ("v1", "First", soonest))

    (move,) = jobs.reschedule_plan(weekend, ledger)

    assert move.moved is False, "it is already on the earliest day there is"


def test_the_queue_comes_forward_in_the_order_it_was_in(tmp_path, config):
    weekend = replace(config, schedule=replace(config.schedule, weekdays_only=False))
    ledger = booked(
        tmp_path,
        ("v2", "Second", datetime(2099, 8, 25, 10, tzinfo=ET)),
        ("v1", "First", datetime(2099, 8, 24, 10, tzinfo=ET)),
    )

    moves = jobs.reschedule_plan(weekend, ledger)

    assert [move.title for move in moves] == ["First", "Second"]
    assert moves[0].now < moves[1].now
    assert all(move.moved for move in moves), "2099 is not the earliest day there is"


def test_the_probe_drops_exactly_the_field_it_names():
    """Each shape differs from the last by one thing, so the first failure and
    the first success bracket the offending field between them."""
    full = {"blogId": "b", "locationId": "l", "urlSlug": "s", "rawHTML": "<p>x</p>"}

    assert jobs._less(full, "locationId") == {
        "blogId": "b", "urlSlug": "s", "rawHTML": "<p>x</p>",
    }
    assert jobs._less(full, "urlSlug", "rawHTML") == {"blogId": "b", "locationId": "l"}
    assert jobs._less(full) == full
    assert full == {"blogId": "b", "locationId": "l", "urlSlug": "s", "rawHTML": "<p>x</p>"}, (
        "the original must not be mutated - every shape is built from it"
    )


# ------------------------------------------- GHL falling over on its own code

# Re-dating an already-scheduled post comes back 400 with `Cannot read
# properties of undefined (reading 'childTaskError')` - a null dereference
# inside GHL, not a field they objected to. Dropping to draft and scheduling
# again is the transition their code survives.

CRASH = (
    'PUT /blogs/posts/p1 -> HTTP 400: {"status":400,"message":"Error while blog '
    'update","name":"HttpException","error":"Cannot read properties of undefined '
    "(reading 'childTaskError')\"}"
)


class Updates:
    """A GHL client that fails the calls you tell it to, and remembers them."""

    def __init__(self, fail_when=lambda call, payload: False):
        self.calls = []
        self.fail_when = fail_when

    def update_post(self, post_id, payload):
        self.calls.append(dict(payload))
        if self.fail_when(len(self.calls), payload):
            raise ghl.GHLError(CRASH)
        return {}


def probe_entry(scheduled_at="2099-08-24T10:00:00-04:00"):
    return SimpleNamespace(ghl_post_id="p1", scheduled_at=scheduled_at, title="A Post")


def moved_payload(when="2099-08-22T14:00:00.000Z"):
    return {"blogId": "b", "rawHTML": "<p>x</p>", "status": "SCHEDULED", "publishedAt": when}


def test_the_straight_update_is_what_gets_tried_first():
    """It is the call that ought to work, and the one that starts working if
    GHL ever fixes it."""
    client = Updates()

    publisher.send_update(client, probe_entry(), moved_payload())

    assert len(client.calls) == 1


def test_their_crash_is_worked_around_by_dropping_to_draft_first():
    client = Updates(fail_when=lambda call, _payload: call == 1)

    publisher.send_update(client, probe_entry(), moved_payload())

    assert [call["status"] for call in client.calls] == ["SCHEDULED", "DRAFT", "SCHEDULED"]
    assert "publishedAt" not in client.calls[1], "a draft carries no date"
    assert client.calls[2]["publishedAt"] == "2099-08-22T14:00:00.000Z"


def test_any_other_error_is_raised_rather_than_worked_around():
    """The detour is for one known crash. Everything else is a real error and
    hiding it behind two more writes would be worse."""

    class Other:
        calls = []

        def update_post(self, post_id, payload):
            raise ghl.GHLError("HTTP 401: unauthorized")

    with pytest.raises(ghl.GHLError, match="401"):
        publisher.send_update(Other(), probe_entry(), moved_payload())


def test_a_post_is_never_left_sitting_as_a_draft():
    """If the reschedule half fails, the post goes back on the day it was
    already on. Silently unscheduling somebody's article is far worse than
    not moving it."""
    client = Updates(fail_when=lambda call, _payload: call in (1, 3))

    with pytest.raises(ghl.GHLError):
        publisher.send_update(client, probe_entry(), moved_payload())

    assert client.calls[-1]["status"] == "SCHEDULED"
    assert client.calls[-1]["publishedAt"] == "2099-08-24T14:00:00.000Z", "its original day"


def test_with_no_original_date_there_is_nothing_to_put_back():
    client = Updates(fail_when=lambda call, _payload: call in (1, 3))

    with pytest.raises(ghl.GHLError):
        publisher.send_update(client, probe_entry(scheduled_at=None), moved_payload())

    assert len(client.calls) == 3, "tried, dropped to draft, tried again - no restore"


def test_publishing_a_due_post_survives_the_same_crash(tmp_path):
    """The publisher loop uses the same endpoint to take a post live. If that
    met the crash unprotected, scheduled posts would silently never go out -
    which is the exact failure the publisher exists to fix."""
    import json

    payload_file = tmp_path / "p.json"
    payload_file.write_text(json.dumps({"rawHTML": "<h1>A</h1>", "status": "SCHEDULED"}))
    ledger = Ledger(path=tmp_path / "ledger.json")
    entry = ledger.record(
        video_id="v1", title="A", url_slug="a", ghl_post_id="p1",
        scheduled_at=datetime(2099, 8, 18, 10, tzinfo=ET), payload_path=str(payload_file),
    )
    client = Updates(fail_when=lambda call, _payload: call == 1)

    publisher.publish_entry(client, entry)

    assert [call["status"] for call in client.calls] == ["PUBLISHED", "DRAFT", "PUBLISHED"]


# ------------------------------------------------- moving the cards by hand


class Asked:
    """A responder that keeps what it was told and always says yes."""

    requester_id = 1

    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, file=None, view=None):
        self.messages.append(content or "")


def run_move(named, monkeypatch):
    """`trello move <named>`, with the board and the button stubbed out."""
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client as bot_client

    read, walked = [], []

    def waiting(config, step, **kw):
        read.append(step)
        return [f"{step} card"], []

    def walk(config, step, **kw):
        walked.append(step)
        return 1, []

    class Pressed:
        def __init__(self, **kw):
            self.confirmed = True

        async def wait(self):
            return None

    monkeypatch.setattr(bot_client.jobs, "moves_waiting", waiting)
    monkeypatch.setattr(bot_client.jobs, "walk_board", walk)
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)

    heard = Asked()
    config = SimpleNamespace(discord=SimpleNamespace(approval_timeout_seconds=1))
    asyncio.run(bot_client._move_cards(heard, config, named))
    return read, walked, heard.messages


def test_move_done_finishes_ads_and_lead_order_too(monkeypatch):
    """The clock splits the two Done steps by the hour their work stops.
    Somebody typing "move done" is asking for the cards to be put away, and
    being told nothing was waiting while two sit in Quality Check is the
    command failing at the only thing it is for."""
    from wilbyte import dailyops

    read, walked, _said = run_move("done", monkeypatch)

    assert read == list(dailyops.DONE_STEPS)
    assert walked == list(dailyops.DONE_STEPS)


def test_move_done_counts_both_steps_in_what_it_says(monkeypatch):
    _read, _walked, said = run_move("done", monkeypatch)

    assert "Moved 2 card(s)" in said[-1]


def test_the_other_moves_are_still_one_step(monkeypatch):
    read, walked, _said = run_move("quality check", monkeypatch)

    assert read == ["to_quality_check"]
    assert walked == ["to_quality_check"]


def test_the_typed_rollover_carries_an_item_however_long_it_has_waited(monkeypatch):
    """"Just carry unticked task for all card." No button to press, no line
    left behind for somebody to drag across in Trello by hand."""
    import asyncio
    from datetime import date
    from types import SimpleNamespace

    from wilbyte import dailyops
    from wilbyte.bot import client as bot_client

    plan = dailyops.RolloverPlan(kind="general", from_title="a", to_title="b")
    plan.leftovers = [
        dailyops.Leftover(person="Nicole", name="one"),
        dailyops.Leftover(person="Nicole", name="two", times_rolled=6),
    ]
    pressed, written = [], []

    class Pressed:
        def __init__(self, **kw):
            pressed.append(kw.get("label", ""))
            self.confirmed = True

        async def wait(self):
            return None

    monkeypatch.setattr(bot_client.jobs, "board_day", lambda config: date(2026, 9, 3))
    monkeypatch.setattr(
        bot_client.jobs, "read_rollover", lambda config, **kw: ([plan], [], {})
    )
    monkeypatch.setattr(
        bot_client.jobs,
        "apply_rollover",
        lambda config, plans, targets, **kw: (written.append(plans), (2, []))[1],
    )
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)

    heard = Asked()
    config = SimpleNamespace(
        discord=SimpleNamespace(approval_timeout_seconds=1),
        schedule=SimpleNamespace(timezone="America/New_York"),
    )
    asyncio.run(bot_client._rollover(heard, config, named=""))

    # One button, and it carries both.
    assert pressed == ["Trello rollover — 2 item(s)"]
    assert "Carried 2 item(s)" in heard.messages[-1]
    assert "carried 6 days running" in "\n".join(heard.messages)


def test_the_window_is_not_three_thousand_lines_of_200_ok(monkeypatch):
    """The agent watcher costs sixteen requests every twenty seconds. At that
    volume httpx's own logging is a window nobody reads - and the two real
    failures so far each sat buried in thousands of lines of it."""
    import logging

    from wilbyte.bot.client import _quieten_http

    monkeypatch.delenv("RYTE_LOG_HTTP", raising=False)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.NOTSET)

    _quieten_http()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_the_requests_can_be_put_back_when_an_api_is_the_problem(monkeypatch):
    import logging

    from wilbyte.bot.client import _quieten_http

    monkeypatch.setenv("RYTE_LOG_HTTP", "true")
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    _quieten_http()

    assert logging.getLogger("httpx").level == logging.NOTSET


# ------------------------------------------------- a payment landing live


PAYRA = """New Payment | Payra 💰

**NAME:** dave luft
**EMAIL:** daveluft@gmail.com
**PHONE:** +13195551212
**AMOUNT:** $606.51
**PRODUCT:** Aged Leads (FEX)"""


def paying(channel_id="777", *, text=PAYRA, at=datetime(2026, 8, 14, 15, 4, tzinfo=timezone.utc)):
    """A Payra notification, as it arrives: another bot, embed, no mention."""
    return SimpleNamespace(
        content="",
        embeds=[SimpleNamespace(url=None, title=None, description=text, author=None)],
        mentions=[],
        mention_everyone=False,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=999, bot=True),
        created_at=at,
    )


class Listening:
    """Somewhere for RYTE's notes to go, and a note of what went there."""

    def __init__(self):
        self.said = []

    async def send(self, content=None, **kwargs):
        self.said.append(content if content is not None else kwargs.get("embed"))
        return SimpleNamespace(id=1)


class PaymentBot:
    def __init__(self, config, channel):
        self.config = config
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


def watching_payments(config, channel_id="777"):
    secrets = replace(
        config.secrets,
        discord_payment_channel_id=channel_id,
        discord_board_channel_id="888",
        levinson_sheet_id="sheet",
    )
    return replace(config, secrets=secrets)


DAVE = [
    SimpleNamespace(name="dave luft", email="daveluft@gmail.com", phone="", source="ghl")
]


def levinson_bot(config, monkeypatch, *, members=None, written=(1, [])):
    """A bot wired to a fake member list and a fake sheet. (bot, channel, writes)"""
    from wilbyte import levinson as levinson_mod

    people = [
        levinson_mod.Member(name=one.name, email=one.email, phone=one.phone, source=one.source)
        for one in (DAVE if members is None else members)
    ]
    writes = []

    def wrote(cfg, batches):
        writes.append(batches)
        return written

    monkeypatch.setattr(jobs, "levinson_members", lambda cfg: (people, []))
    monkeypatch.setattr(jobs, "write_levinson", wrote)

    channel = Listening()
    return PaymentBot(watching_payments(config), channel), channel, writes


def test_the_payment_channel_is_acted_on(config):
    from wilbyte.bot.client import is_payment

    assert is_payment(paying("777"), watching_payments(config, "777"))


def test_payments_elsewhere_are_not_the_payment_channel(config):
    from wilbyte.bot.client import is_payment

    assert not is_payment(paying("666"), watching_payments(config, "777"))


def test_no_payment_channel_means_nothing_is_one(config):
    from wilbyte.bot.client import is_payment

    assert not is_payment(paying("777"), config)


def test_a_levinson_agents_payment_goes_on_that_months_tab(config, monkeypatch):
    from wilbyte.bot.client import handle_payment

    bot, channel, writes = levinson_bot(config, monkeypatch)

    asyncio.run(handle_payment(bot, paying()))

    (batches,) = writes
    ((month, lines),) = batches
    # 15:04 UTC on the 14th is still the 14th in New York; the month is the
    # month the money arrived in, not the month somebody happens to run this.
    assert month == (2026, 8)
    assert [(one.name, one.amount) for one in lines] == [("Dave Luft", "$606.51")]
    assert "Dave Luft" in channel.said[0] and "August 2026" in channel.said[0]


def test_a_payment_from_somebody_else_leaves_no_trace(config, monkeypatch):
    """Most of the channel is not Levinson's. A row for each of those would be
    wrong, and a message about each of those would bury the ones that aren't."""
    from wilbyte.bot.client import handle_payment

    stranger = [
        SimpleNamespace(
            name="Someone Else", email="someone@else.com", phone="", source="ghl"
        )
    ]
    bot, channel, writes = levinson_bot(config, monkeypatch, members=stranger)

    asyncio.run(handle_payment(bot, paying()))

    assert writes == []
    assert channel.said == []


def test_a_message_that_is_not_a_payment_is_not_looked_up(config, monkeypatch):
    """Somebody says "nice" in the channel. That is not two hundred requests
    to GoHighLevel."""
    from wilbyte.bot.client import handle_payment

    bot, channel, writes = levinson_bot(config, monkeypatch)

    def never(cfg):
        raise AssertionError("read the member list for a message with no payment in it")

    monkeypatch.setattr(jobs, "levinson_members", never)

    asyncio.run(handle_payment(bot, paying(text="nice 🎉")))

    assert writes == [] and channel.said == []


def test_the_member_list_is_read_once_and_kept(config, monkeypatch):
    """Walking the contacts is two hundred requests. Two payments a minute
    apart is one walk."""
    from wilbyte.bot.client import handle_payment

    bot, _, writes = levinson_bot(config, monkeypatch)
    reads = []
    people = jobs.levinson_members(bot.config)[0]
    monkeypatch.setattr(
        jobs, "levinson_members", lambda cfg: (reads.append(1), (people, []))[1]
    )

    asyncio.run(handle_payment(bot, paying()))
    asyncio.run(handle_payment(bot, paying()))

    assert len(reads) == 1
    assert len(writes) == 2


def test_the_kept_member_list_goes_stale(config, monkeypatch):
    """Somebody who opted in this morning has to arrive eventually."""
    from wilbyte.bot import client
    from wilbyte.bot.client import handle_payment

    bot, _, _ = levinson_bot(config, monkeypatch)
    reads = []
    people = jobs.levinson_members(bot.config)[0]
    monkeypatch.setattr(
        jobs, "levinson_members", lambda cfg: (reads.append(1), (people, []))[1]
    )

    asyncio.run(handle_payment(bot, paying()))
    bot._levinson_members_at -= client.MEMBERS_GOOD_FOR + 1
    asyncio.run(handle_payment(bot, paying()))

    assert len(reads) == 2


def test_no_member_list_at_all_is_said_rather_than_swallowed(config, monkeypatch):
    """An empty list is not "not a Levinson agent", it is not knowing - and a
    payment quietly dropped is the whole failure this report exists to avoid."""
    from wilbyte.bot.client import handle_payment

    bot, channel, writes = levinson_bot(config, monkeypatch, members=[])
    monkeypatch.setattr(
        jobs, "levinson_members", lambda cfg: ([], ["GHL_API_TOKEN isn't set."])
    )

    asyncio.run(handle_payment(bot, paying()))

    assert writes == []
    assert "GHL_API_TOKEN" in str(channel.said[0].description)


def test_the_same_complaint_is_not_made_for_every_payment(config, monkeypatch):
    """A thousand messages a month through this channel. One warning an hour."""
    from wilbyte.bot.client import handle_payment

    bot, channel, _ = levinson_bot(config, monkeypatch, members=[])
    monkeypatch.setattr(jobs, "levinson_members", lambda cfg: ([], ["nothing came back."]))

    for _ in range(5):
        asyncio.run(handle_payment(bot, paying()))

    assert len(channel.said) == 1


def test_the_complaint_comes_back_after_an_hour(config, monkeypatch):
    from wilbyte.bot import client
    from wilbyte.bot.client import handle_payment

    bot, channel, _ = levinson_bot(config, monkeypatch, members=[])
    monkeypatch.setattr(jobs, "levinson_members", lambda cfg: ([], ["nothing came back."]))

    asyncio.run(handle_payment(bot, paying()))
    bot._grumbles["levinson-members"] -= client.SAY_AGAIN_AFTER + 1
    asyncio.run(handle_payment(bot, paying()))

    assert len(channel.said) == 2


def test_a_sheet_that_refuses_the_row_is_reported(config, monkeypatch):
    from wilbyte.bot.client import handle_payment

    bot, channel, _ = levinson_bot(
        config, monkeypatch, written=(0, ["The tracker didn't accept the row."])
    )

    asyncio.run(handle_payment(bot, paying()))

    assert "didn't accept" in str(channel.said[0].description)


def test_a_broken_member_lookup_is_said_rather_than_swallowed(config, monkeypatch):
    """GoHighLevel falling over looks exactly like a quiet month otherwise."""
    from wilbyte.bot.client import handle_payment

    bot, channel, writes = levinson_bot(config, monkeypatch)

    def blew_up(cfg):
        raise RuntimeError("GoHighLevel said 500")

    monkeypatch.setattr(jobs, "levinson_members", blew_up)

    asyncio.run(handle_payment(bot, paying()))

    assert writes == []
    assert "500" in str(channel.said[0].description)


# ------------------------------------------------- spreading against their own card


AGENT_URL = "https://trello.com/c/siona"


class SpreadBoard:
    """Enough of a board to spread one agent off a setup card."""

    def __init__(self, *, on_setup, their_card, checklists=("OTP IUL Plus",)):
        self.on_setup = on_setup
        self.their_card = their_card
        self.written = []
        self.checklists = list(checklists)
        self.closed = False

    def board_lists(self, _board_id):
        return [{"id": "L"}]

    def list_cards(self, _list_id):
        return [
            {"id": "setup", "name": "Agent Setup Going Live Friday 09/04"},
            {"id": "order", "name": "Lead Order 09/04/26"},
            {"id": "agent", "name": "New Agent - Siona Paradas",
             "url": AGENT_URL, "shortUrl": AGENT_URL},
        ]

    def card_checklists(self, card_id):
        if card_id == "setup":
            return [{"id": "s1", "name": "Therese", "checkItems": [
                {"name": f"{AGENT_URL} {self.on_setup}"}
            ]}]
        return [
            {"id": f"c{n}", "name": name, "checkItems": []}
            for n, name in enumerate(self.checklists)
        ]

    def card_detail(self, card_id):
        return {"id": card_id, "desc": self.their_card}

    def add_check_item(self, checklist_id, name, **kwargs):
        self.written.append((checklist_id, name))
        return {"id": "i1"}

    def close(self):
        self.closed = True


SIONA_CARD = """-- New Client Onboarded --

First Name: Siona
Last Name: Paradas
Lead Type: Index Universal Life

OTP SPANISH IUL
"""


def spreading(board, monkeypatch, config):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 3))
    return jobs.spread_to_lead_order(config)


def test_a_line_whose_card_disagrees_is_placed_and_questioned(config, monkeypatch):
    """The setup card said "OTP IUL" and her own card says Spanish. The line
    goes where the setup card says - that is what the spread is for - and the
    disagreement is raised rather than swallowed."""
    board = SpreadBoard(on_setup="OTP IUL", their_card=SIONA_CARD)

    added, conflicts, problems = spreading(board, monkeypatch, config)

    assert problems == []
    assert board.written == [("c0", f"{AGENT_URL} OTP IUL")]
    assert [line for line in added if "Siona" in line]
    (clash,) = conflicts
    assert clash["agent"] == "New Agent - Siona Paradas"
    assert clash["checklist"] == "OTP IUL Plus"
    assert clash["setup"] == "OTP IUL"
    assert clash["ordered"] == "OTP SPANISH IUL"
    assert clash["url"] == AGENT_URL


def test_a_card_that_agrees_is_not_questioned(config, monkeypatch):
    board = SpreadBoard(
        on_setup="OTP SPANISH IUL",
        their_card=SIONA_CARD,
        checklists=("OTP SPANISH IUL",),
    )

    added, conflicts, problems = spreading(board, monkeypatch, config)

    assert conflicts == [] and problems == []
    assert board.written == [("c0", f"{AGENT_URL} OTP SPANISH IUL")]


def test_a_card_nobody_can_read_is_not_a_conflict(config, monkeypatch):
    """Trello having a bad second is not somebody's leads going astray, and
    saying it is teaches people to skip the message."""
    board = SpreadBoard(on_setup="OTP IUL", their_card=SIONA_CARD)
    board.card_detail = lambda card_id: (_ for _ in ()).throw(RuntimeError("HTTP 503"))

    added, conflicts, problems = spreading(board, monkeypatch, config)

    assert conflicts == [] and problems == []
    assert len(board.written) == 1


def test_a_line_that_could_not_be_placed_is_never_questioned(config, monkeypatch):
    """Nothing was written, so there is nothing to disagree about - and the
    unplaced line is already reported on its own."""
    board = SpreadBoard(
        on_setup="OTP IUL", their_card=SIONA_CARD, checklists=("OTP Widows",)
    )

    added, conflicts, problems = spreading(board, monkeypatch, config)

    assert board.written == [] and conflicts == []
    assert "doesn't match any checklist" in problems[0]


def test_the_conflict_reads_as_a_question_rather_than_a_fault(config, monkeypatch):
    board = SpreadBoard(on_setup="OTP IUL", their_card=SIONA_CARD)
    _, conflicts, _ = spreading(board, monkeypatch, config)

    card = embeds.spread_conflicts(conflicts, shown=10)

    assert "OTP IUL Plus" in card.description
    assert "OTP SPANISH IUL" in card.description
    assert card.colour.value == embeds.AMBER


def test_a_self_contradicting_confirmation_is_not_an_accusation():
    """"Set up on the wrong leads" points at whoever did the setup. When the
    confirmation's own body names what the agent ordered, the setup was right
    and a sentence wasn't finished - which is a different message."""
    card = {
        "agent": "Eduardo Munoz", "url": "https://trello.com/c/e", "when": "tomorrow",
        "ordered": "Basic/Instant Spanish IUL", "setup": "OTP VET",
        "also": "SPANISH IUL", "typo": True,
    }

    said = embeds.wrong_setups([card], shown=10)

    assert "disagrees with itself" in said.author.name
    assert said.colour.value == embeds.AMBER
    assert "which is what they ordered" in said.description


def test_leads_in_the_wrong_campaign_still_reads_as_one():
    card = {
        "agent": "Someone", "url": "", "when": "today",
        "ordered": "OTP VETS", "setup": "OTP FEX", "also": "", "typo": False,
    }

    said = embeds.wrong_setups([card], shown=10)

    assert "set up on the wrong leads" in said.author.name
    assert said.colour.value == embeds.RED


def test_one_of_each_is_reported_as_the_serious_one():
    """A batch with a real wrong setup in it is not softened by the typo
    beside it."""
    cards = [
        {"agent": "A", "url": "", "when": "today", "ordered": "OTP VETS",
         "setup": "OTP FEX", "also": "", "typo": False},
        {"agent": "B", "url": "", "when": "today", "ordered": "Spanish IUL",
         "setup": "OTP VET", "also": "SPANISH IUL", "typo": True},
    ]

    said = embeds.wrong_setups(cards, shown=10)

    assert said.colour.value == embeds.RED
    assert "set up wrong" in said.title


# ------------------------------------------------- unticked, on a day you name


def _unticked(monkeypatch, config, said, *, found=()):
    """Run the handler and report what reached the board. (asked, sent)"""
    from wilbyte.bot import client

    asked = {}

    def reading(cfg, *, day=None, and_tomorrow=True):
        asked["day"], asked["and_tomorrow"] = day, and_tomorrow
        return list(found), []

    monkeypatch.setattr(jobs, "unmarked_agents", reading)
    monkeypatch.setattr(client, "_today", lambda cfg: date(2026, 9, 4))
    heard = Listening()
    asyncio.run(client._send_unticked(ChannelResponder(heard), config, said))
    return asked, heard.said


def test_a_day_named_on_unticked_reaches_the_board(config, monkeypatch):
    """"unticked yesterday" used to be answered about today, with nothing in
    the reply admitting a different question had been asked."""
    asked, _ = _unticked(monkeypatch, config, "unticked yesterday")

    assert asked["day"] == date(2026, 9, 3)


def test_a_named_day_is_asked_about_on_its_own(config, monkeypatch):
    """"Unticked 09/03" is a question about the third. Answering it about the
    third and the fourth answers a question nobody asked - the more so when
    the fourth is today."""
    asked, _ = _unticked(monkeypatch, config, "unticked 09/03")

    assert asked["day"] == date(2026, 9, 3)
    assert asked["and_tomorrow"] is False


def test_no_day_named_still_means_today_and_tomorrow(config, monkeypatch):
    """The afternoon check is about what is running out of time, so it keeps
    both days."""
    asked, _ = _unticked(monkeypatch, config, "unticked")

    assert asked["day"] is None
    assert asked["and_tomorrow"] is True


def test_the_answer_names_the_day_it_covered(config, monkeypatch):
    _, sent = _unticked(
        monkeypatch, config, "unticked yesterday",
        found=[{"name": "New Agent - Someone", "url": "", "when": "Thu Sep 03"}],
    )

    (card,) = sent
    assert "Thu Sep 03" in card.author.name
    assert "or Fri Sep 04" not in card.author.name


def test_nothing_outstanding_says_which_day_that_was(config, monkeypatch):
    _, sent = _unticked(monkeypatch, config, "unticked yesterday")

    assert "Thu Sep 03" in sent[0]


def test_the_start_day_is_read_on_the_boards_clock(config, monkeypatch):
    """"start monday" decides which day a blog post may land on, and the
    posting schedule is Eastern. Read off a Mac in Manila it picks the wrong
    Monday for half of every day."""
    from wilbyte import prefs
    from wilbyte.bot import client

    seen = {}

    def parsing(text, *, today=None):
        seen["today"] = today
        raise prefs.PrefsError("stop here — the clock it read is the point")

    monkeypatch.setattr(prefs, "parse_day", parsing)
    monkeypatch.setattr(client, "_today", lambda cfg: date(2026, 9, 4))

    asyncio.run(
        client._set_earliest_day(ChannelResponder(Listening()), config, "monday")
    )

    assert seen["today"] == date(2026, 9, 4)
