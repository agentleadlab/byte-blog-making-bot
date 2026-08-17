"""RYTE publishing its own scheduled posts, because GHL's scheduler won't.

Reading the blog back settled it: every post that ever went out on its day
carries `metaData.scheduledBy` and a pair of task ids, and every post created
through the API carries neither. GHL stores a SCHEDULED post and then has
nothing queued to publish it.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from wilbyte import ghl, publisher
from wilbyte.state import Ledger

NOW = datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)


def payload_file(tmp_path, slug="a-post"):
    path = tmp_path / f"{slug}.json"
    path.write_text(
        json.dumps({"title": "A Post", "rawHTML": "<h1>A Post</h1>", "status": "SCHEDULED"})
    )
    return path


def ledger_with(tmp_path, *, scheduled_at, published_at=None, post_id="post1", payload=True):
    ledger = Ledger(path=tmp_path / "ledger.json")
    ledger.record(
        video_id="vid1",
        title="A Post",
        url_slug="a-post",
        scheduled_at=scheduled_at,
        ghl_post_id=post_id,
        payload_path=str(payload_file(tmp_path)) if payload else None,
        published_at=published_at,
    )
    return ledger


class FakeClient:
    def __init__(self, fail=False):
        self.updates = []
        self.fail = fail

    def update_post(self, post_id, payload):
        if self.fail:
            raise ghl.GHLError("PUT /blogs/posts/post1 -> HTTP 500")
        self.updates.append((post_id, payload))
        return {}

    def close(self):
        pass


# ------------------------------------------------------------------ what is due


def test_a_post_whose_slot_has_passed_is_due(tmp_path):
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=5))

    assert [e.video_id for e in publisher.due(ledger, now=NOW)] == ["vid1"]


def test_a_post_whose_slot_is_still_ahead_is_not_due(tmp_path):
    ledger = ledger_with(tmp_path, scheduled_at=NOW + timedelta(hours=1))

    assert publisher.due(ledger, now=NOW) == []


def test_an_already_published_post_is_never_due_again(tmp_path):
    """The loop runs every minute; without this it would republish forever."""
    ledger = ledger_with(
        tmp_path, scheduled_at=NOW - timedelta(days=2), published_at=NOW - timedelta(days=2)
    )

    assert publisher.due(ledger, now=NOW) == []


def test_a_draft_is_never_due(tmp_path):
    ledger = ledger_with(tmp_path, scheduled_at=None)

    assert publisher.due(ledger, now=NOW) == []


def test_a_post_with_no_ghl_id_is_not_due(tmp_path):
    """There is nothing to address the update to."""
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(hours=1), post_id=None)

    assert publisher.due(ledger, now=NOW) == []


def test_overdue_posts_are_caught_up_rather_than_skipped(tmp_path):
    """A post that goes out late is a post that goes out."""
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(days=4))

    assert len(publisher.due(ledger, now=NOW)) == 1


# ------------------------------------------------------------------ publishing


def test_publishing_sends_the_whole_saved_body_back(tmp_path, config):
    """GHL's update replaces the post, and its listing omits `rawHTML` - a PUT
    without the body would blank the article."""
    slot = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)  # 10:00 Eastern
    ledger = ledger_with(tmp_path, scheduled_at=slot)
    client = FakeClient()

    published, problems = publisher.publish_due(config, ledger, now=NOW, client=client)

    assert problems == []
    assert [e.video_id for e in published] == ["vid1"]
    post_id, body = client.updates[0]
    assert post_id == "post1"
    assert body["rawHTML"] == "<h1>A Post</h1>"
    assert body["status"] == ghl.STATUS_PUBLISHED
    # The slot it was meant for, not the minute RYTE got around to it.
    assert body["publishedAt"] == "2026-08-18T14:00:00.000Z"


def test_the_ledger_records_the_publish_so_it_happens_once(tmp_path, config):
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=1))
    client = FakeClient()

    publisher.publish_due(config, ledger, now=NOW, client=client)
    publisher.publish_due(config, ledger, now=NOW, client=client)

    assert len(client.updates) == 1
    assert ledger.entries["vid1"].published_at


def test_the_publish_survives_a_restart(tmp_path, config):
    """The mark has to be on disk, not just in memory - RYTE gets restarted."""
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=1))
    publisher.publish_due(config, ledger, now=NOW, client=FakeClient())

    reloaded = Ledger.load(tmp_path / "ledger.json")

    assert publisher.due(reloaded, now=NOW) == []


def test_a_failed_publish_is_reported_and_not_marked_done(tmp_path, config):
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=1))

    published, problems = publisher.publish_due(
        config, ledger, now=NOW, client=FakeClient(fail=True)
    )

    assert published == []
    assert "HTTP 500" in problems[0]
    assert not ledger.entries["vid1"].published_at, "it must be retried next minute"


def test_a_post_saved_before_bodies_were_kept_says_so(tmp_path, config):
    """Better than a PUT that would empty the article."""
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=1), payload=False)
    client = FakeClient()

    published, problems = publisher.publish_due(config, ledger, now=NOW, client=client)

    assert published == []
    assert client.updates == []
    assert "by hand" in problems[0]


def test_one_bad_post_does_not_block_the_others(tmp_path, config):
    ledger = ledger_with(tmp_path, scheduled_at=NOW - timedelta(minutes=1))
    ledger.record(
        video_id="vid2",
        title="Broken",
        url_slug="broken",
        scheduled_at=NOW - timedelta(minutes=1),
        ghl_post_id="post2",
        payload_path=None,
    )

    published, problems = publisher.publish_due(config, ledger, now=NOW, client=FakeClient())

    assert [e.video_id for e in published] == ["vid1"]
    assert len(problems) == 1


def test_next_due_reports_the_soonest_unpublished_slot(tmp_path):
    ledger = ledger_with(tmp_path, scheduled_at=NOW + timedelta(hours=2))

    assert publisher.next_due(ledger, now=NOW) == NOW + timedelta(hours=2)


def test_next_due_is_none_when_nothing_is_waiting(tmp_path):
    ledger = ledger_with(
        tmp_path, scheduled_at=NOW - timedelta(days=1), published_at=NOW - timedelta(days=1)
    )

    assert publisher.next_due(ledger, now=NOW) is None
