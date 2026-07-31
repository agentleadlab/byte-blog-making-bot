from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from wilbyte.ghl import GHLError, build_post_payload, resolve_by_name, to_api_timestamp


def test_timestamp_converts_local_10am_to_utc():
    moment = datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("America/Chicago"))

    assert to_api_timestamp(moment) == "2026-08-12T15:00:00.000Z"


def test_naive_datetime_is_refused():
    with pytest.raises(GHLError):
        to_api_timestamp(datetime(2026, 8, 12, 10, 0))


def test_payload_contains_every_field_wil_fills_in_by_hand():
    payload = build_post_payload(
        location_id="LOC",
        blog_id="BLOG",
        title="Why Most Agents Never Move Up a Lead Tier",
        content_html="<h1>x</h1>",
        description="Aged leads buy skill.",
        url_slug="insurance-lead-progression-roadmap",
        canonical_link="https://agentleadlab.com/post/insurance-lead-progression-roadmap",
        author_id="AUTHOR",
        category_ids=["CAT"],
        keywords=["Lead Lab", "Agent Lead Lab"],
        image_url="https://cdn/img.png",
        image_alt="insurance-lead-progression-roadmap",
        status="SCHEDULED",
        published_at=datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("America/Chicago")),
    )

    assert payload["locationId"] == "LOC"
    assert payload["blogId"] == "BLOG"
    assert payload["urlSlug"] == "insurance-lead-progression-roadmap"
    assert payload["categories"] == ["CAT"]
    assert payload["author"] == "AUTHOR"
    assert payload["tags"] == ["Lead Lab", "Agent Lead Lab"]
    assert payload["status"] == "SCHEDULED"
    assert payload["publishedAt"] == "2026-08-12T15:00:00.000Z"
    assert payload["imageAltText"] == "insurance-lead-progression-roadmap"


def test_payload_omits_image_url_when_there_is_no_cover_yet():
    payload = build_post_payload(
        location_id="LOC", blog_id="BLOG", title="t", content_html="<p>x</p>",
        description="d", url_slug="s", canonical_link="c", author_id="a",
        category_ids=["c"], keywords=[], image_url=None, image_alt="",
        status="DRAFT", published_at=None,
    )

    assert "imageUrl" not in payload
    assert "publishedAt" not in payload


def test_author_lookup_ignores_smart_quotes_and_case():
    """GHL shows 'Arnold "Tre" Tarpley'; the config may use straight or curly quotes."""
    authors = [
        {"_id": "a1", "name": "Someone Else"},
        {"_id": "a2", "name": "Arnold “Tre” Tarpley"},
    ]

    assert resolve_by_name(authors, 'Arnold "Tre" Tarpley', kind="author") == "a2"


def test_category_lookup_by_name():
    assert resolve_by_name([{"id": "c9", "name": "LeadLab"}], "LeadLab", kind="category") == "c9"


def test_unknown_name_lists_what_is_available():
    with pytest.raises(GHLError, match="LeadLab"):
        resolve_by_name([{"_id": "c1", "name": "LeadLab"}], "Marketing", kind="category")
