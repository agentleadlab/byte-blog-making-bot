from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from wilbyte.ghl import (
    GHLClient,
    GHLError,
    build_post_payload,
    resolve_by_name,
    to_api_timestamp,
)


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


SITES_ONE = [{"_id": "vVS0DP09w5DdUY9AVVuA", "name": "Agent Lead Lab Blogs"}]
SITES_TWO = SITES_ONE + [{"_id": "other123", "name": "Second Blog"}]


def test_a_matching_blog_id_is_used_without_comment():
    from wilbyte.ghl import resolve_blog_id

    assert resolve_blog_id(SITES_ONE, "vVS0DP09w5DdUY9AVVuA") == ("vVS0DP09w5DdUY9AVVuA", None)


def test_a_mistyped_id_falls_back_to_the_only_blog():
    """These ids are full of 0/O lookalikes; one blog means no ambiguity."""
    from wilbyte.ghl import resolve_blog_id

    blog_id, note = resolve_blog_id(SITES_ONE, "vVSODP09w5DdUY9AVVuA")  # letter O

    assert blog_id == "vVS0DP09w5DdUY9AVVuA"
    assert "only one" in note


def test_an_unset_id_falls_back_to_the_only_blog():
    from wilbyte.ghl import resolve_blog_id

    blog_id, note = resolve_blog_id(SITES_ONE, None)

    assert blog_id == "vVS0DP09w5DdUY9AVVuA"
    assert note


def test_several_blogs_and_a_bad_id_is_an_error_listing_them():
    from wilbyte.ghl import resolve_blog_id

    with pytest.raises(GHLError, match="Second Blog"):
        resolve_blog_id(SITES_TWO, "nope")


def test_several_blogs_with_a_good_id_is_fine():
    from wilbyte.ghl import resolve_blog_id

    assert resolve_blog_id(SITES_TWO, "other123")[0] == "other123"


def test_no_blogs_at_all_is_an_error():
    from wilbyte.ghl import resolve_blog_id

    with pytest.raises(GHLError, match="no blog sites"):
        resolve_blog_id([], None)


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


# ------------------------------------------------------- listing every status


class FakePostsAPI:
    """Stands in for /blogs/posts/all.

    Modelled on what the live endpoint does: asked with no status it answers
    with published posts only, which is how a calendar full of scheduled posts
    came back looking empty.
    """

    def __init__(self, by_status: dict, *, accepts_status: bool = True):
        self.by_status = by_status
        self.accepts_status = accepts_status
        self.calls: list[str | None] = []

    def __call__(self, method, path, params=None, **kwargs):
        status = (params or {}).get("status")
        self.calls.append(status)
        if status and not self.accepts_status:
            raise GHLError("HTTP 422: status is not allowed")
        if status:
            return {"data": list(self.by_status.get(status, []))}
        return {"data": list(self.by_status.get("PUBLISHED", []))}


def client_with(api) -> GHLClient:
    client = GHLClient.__new__(GHLClient)
    client.location_id = "loc"
    client._request = api
    return client


def test_scheduled_posts_are_listed_even_though_the_bare_call_hides_them():
    api = FakePostsAPI({
        "PUBLISHED": [{"_id": "p1"}],
        "SCHEDULED": [{"_id": "s1"}, {"_id": "s2"}],
        "DRAFT": [{"_id": "d1"}],
    })

    posts = client_with(api).list_posts("blog1")

    assert {p["_id"] for p in posts} == {"p1", "s1", "s2", "d1"}


def test_a_post_returned_under_two_statuses_is_only_counted_once():
    api = FakePostsAPI({"PUBLISHED": [{"_id": "p1"}], "SCHEDULED": [{"_id": "p1"}]})

    assert len(client_with(api).list_posts("blog1")) == 1


def test_an_api_that_rejects_the_status_filter_still_returns_what_it_can():
    """Never let a probe for extra posts turn into a failed run."""
    api = FakePostsAPI({"PUBLISHED": [{"_id": "p1"}]}, accepts_status=False)

    assert [p["_id"] for p in client_with(api).list_posts("blog1")] == ["p1"]


def test_posts_without_an_id_are_not_collapsed_into_one():
    api = FakePostsAPI({"PUBLISHED": [{"urlSlug": "a"}, {"urlSlug": "b"}]})

    assert len(client_with(api).list_posts("blog1")) == 2
