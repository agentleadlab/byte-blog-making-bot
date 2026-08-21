"""Bot logic that can be tested without a gateway connection."""
import asyncio

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
