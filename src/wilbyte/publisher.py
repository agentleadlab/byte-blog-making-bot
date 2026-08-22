"""Publish scheduled posts when their slot arrives, because GHL won't.

GoHighLevel's scheduler runs on a background task its Blogs API never creates.
Reading the blog's own posts back settles it: every post that has gone out on
its day carries `metaData.scheduledBy` and a pair of task ids, and every post
created through the API carries neither. GHL accepts `status: SCHEDULED`,
stores the post, shows it in the dashboard with a clock icon - and has nothing
queued to actually publish it. Three posts sat that way and silently missed
their days. Its API won't even list them back.

So RYTE publishes them itself. The ledger already records every slot it handed
out and the id GHL returned, and `publish_post` now saves the exact body it
sent - which is what makes this possible, because the list endpoint does not
return `rawHTML` and a PUT without a body would blank the article.

Anything overdue is published on the next check rather than skipped: a post
that goes out late is a post that goes out, and the alternative is the failure
this exists to fix.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import ghl
from .config import Config
from .scheduler import parse_timestamp
from .state import Ledger, LedgerEntry


class PublishError(RuntimeError):
    """Raised when a due post cannot be published."""


def due(ledger: Ledger, *, now: datetime | None = None) -> list[LedgerEntry]:
    """Every post whose slot has arrived and that RYTE hasn't published yet."""
    moment = now or datetime.now(timezone.utc)
    ready = []
    for entry in ledger.entries.values():
        if entry.published_at or not entry.scheduled_at or not entry.ghl_post_id:
            continue
        slot = parse_timestamp(entry.scheduled_at)
        if slot and slot <= moment:
            ready.append(entry)
    return sorted(ready, key=lambda e: e.scheduled_at or "")


def publish_due(
    config: Config,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    client: ghl.GHLClient | None = None,
) -> tuple[list[LedgerEntry], list[str]]:
    """Publish everything that's due. Returns (published, problems).

    One failure doesn't stop the rest: a missing payload file for one post is
    no reason for the other two to stay unpublished.
    """
    ready = due(ledger, now=now)
    if not ready:
        return [], []

    owned = client is None
    if owned:
        config.secrets.require("ghl_api_token", "ghl_location_id")
        client = ghl.GHLClient(config.secrets.ghl_api_token, config.secrets.ghl_location_id)

    published: list[LedgerEntry] = []
    problems: list[str] = []
    try:
        for entry in ready:
            try:
                publish_entry(client, entry)
            except (PublishError, ghl.GHLError, OSError, ValueError) as exc:
                problems.append(f"{entry.title or entry.url_slug}: {exc}")
                continue
            ledger.mark_published(entry.video_id)
            published.append(entry)
    finally:
        if owned:
            client.close()
        if published:
            ledger.save()
    return published, problems


def publish_entry(client: ghl.GHLClient, entry: LedgerEntry) -> None:
    """Flip one post to PUBLISHED, sending the whole body back with it."""
    payload = load_payload(entry)
    payload[ghl.POST_FIELDS["status"]] = ghl.STATUS_PUBLISHED
    if entry.scheduled_at:
        slot = parse_timestamp(entry.scheduled_at)
        if slot:
            payload[ghl.SCHEDULE_FIELD] = ghl.to_api_timestamp(slot)
    send_update(client, entry, payload)


# GHL's own code falls over updating a post that is already scheduled:
#
#     Cannot read properties of undefined (reading 'childTaskError')
#
# Not a field they rejected - a null dereference in whatever tracks a post's
# scheduling task, which only exists on the way *into* SCHEDULED. Nineteen
# posts were refused for it, and the same endpoint is what takes a post live,
# so publishing can meet it too.
CHILD_TASK_CRASH = "childTaskError"


def send_update(client: ghl.GHLClient, entry: LedgerEntry, payload: dict) -> None:
    """Update a post, going the long way round if GHL crashes on its own task.

    Straight through first, because that is the call that ought to work and
    the one that will start working if GHL ever fixes it. Only their specific
    crash triggers the detour - anything else is a real error and is raised
    rather than buried under two more writes.

    The detour drops the post to a draft and sends the update again, which is
    the transition their code survives. If the second half fails the post is
    put back the way it was found: leaving somebody's article sitting as an
    unscheduled draft, silently, is far worse than not changing it.
    """
    try:
        client.update_post(entry.ghl_post_id, payload)
        return
    except Exception as exc:
        if CHILD_TASK_CRASH not in str(exc):
            raise

    as_draft = {key: value for key, value in payload.items() if key != ghl.SCHEDULE_FIELD}
    as_draft[ghl.POST_FIELDS["status"]] = ghl.STATUS_DRAFT
    client.update_post(entry.ghl_post_id, as_draft)

    try:
        client.update_post(entry.ghl_post_id, payload)
    except Exception:
        was = parse_timestamp(entry.scheduled_at) if entry.scheduled_at else None
        if was:
            client.update_post(
                entry.ghl_post_id,
                {
                    **payload,
                    ghl.POST_FIELDS["status"]: ghl.STATUS_SCHEDULED,
                    ghl.SCHEDULE_FIELD: ghl.to_api_timestamp(was),
                },
            )
        raise


def load_payload(entry: LedgerEntry) -> dict:
    """The exact body RYTE sent when it created the post.

    GHL's update is a replace, and its list endpoint doesn't return `rawHTML`,
    so there is nowhere else to get the article from. A post recorded before
    RYTE started saving these has to be published by hand, and saying so is
    better than sending a PUT that would empty it.
    """
    if not entry.payload_path:
        raise PublishError(
            "no saved payload for this post — it was created before RYTE kept "
            "them. Publish it by hand in GHL."
        )
    path = Path(entry.payload_path)
    if not path.exists():
        raise PublishError(f"saved payload is missing from disk ({path})")
    return json.loads(path.read_text(encoding="utf-8"))


def next_due(ledger: Ledger, *, now: datetime | None = None) -> datetime | None:
    """When the next unpublished post is due, for a 'nothing until X' message."""
    moment = now or datetime.now(timezone.utc)
    upcoming = []
    for entry in ledger.entries.values():
        if entry.published_at or not entry.scheduled_at:
            continue
        slot = parse_timestamp(entry.scheduled_at)
        if slot and slot > moment:
            upcoming.append(slot)
    return min(upcoming) if upcoming else None
