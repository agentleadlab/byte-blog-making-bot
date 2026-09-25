"""What a post does for search, checked before it goes out."""

from __future__ import annotations

from types import SimpleNamespace as NS

from wilbyte import seo

BASE = "https://agentleadlab.com/post/"


def test_a_keyword_is_found_whatever_order_and_plural():
    assert seo.has_keyword("Veteran Life Insurance Leads: Why They Convert", "veteran life insurance lead")
    assert seo.has_keyword("How to buy leads for life insurance veterans", "veteran life insurance leads")
    assert seo.has_keyword("Final expense leads: the cost of a lead", "final expense lead cost")
    assert not seo.has_keyword("The Lead Type That Tells You Why They Called", "veteran life insurance leads")
    assert not seo.has_keyword("anything", "")


def test_the_opening_is_after_the_h1():
    html = "<h1>Veteran Leads</h1><p>" + "word " * 120 + "veteran</p>"

    assert not seo.has_keyword(seo.opening(html), "veteran leads")
    assert seo.has_keyword(seo.opening("<h1>x</h1><p>Veteran leads are different.</p>"), "veteran leads")


def test_links_to_posts_that_dont_exist_lose_the_link_not_the_words():
    html = ('<p><a href="https://agentleadlab.com/post/real/">Real</a>, '
            '<a href="https://www.agentleadlab.com/post/fake?x=1">Fake</a>, '
            '<a href="https://agentleadlab.com/schedule-a-call">call</a>, '
            '<a href="https://leadlabcrm.com">CRM</a></p>')

    kept, gone = seo.keep_known_links(html, ["https://agentleadlab.com/post/real"], BASE)

    assert gone == ["https://www.agentleadlab.com/post/fake?x=1"]
    assert ">Fake<" not in kept and "Fake," in kept
    assert kept.count("<a ") == 3
    assert seo.site_links(kept, BASE) == ["https://agentleadlab.com/post/real/"]


def test_tags_are_the_posts_own_then_the_brands_each_once():
    assert seo.tags(["agentleadlab", "vet leads"], "Veteran  Life Insurance Leads", ["vet leads", ""]) == [
        "veteran life insurance leads", "vet leads", "agentleadlab"]


def test_the_check_says_where_the_keyword_is_missing():
    report = seo.check(
        keyword="veteran life insurance leads",
        title="The Lead Type That Tells You Why They Called",
        slug="veteran-life-insurance-leads-convert",
        h1="Veteran Life Insurance Leads: Why They Convert",
        html="<h1>Veteran Life Insurance Leads</h1><p>Veteran life insurance leads convert.</p>"
             "<h2>Why veteran life insurance leads close</h2>"
             '<p><a href="https://agentleadlab.com/post/aged-vs-fresh">aged vs fresh</a></p>',
        description="Why veteran life insurance leads convert so well.",
        alt="veteran-life-insurance-leads-convert", base=BASE,
    )

    assert report.missing == ["title"]
    assert report.internal == 1
    assert report.warnings() == ['SEO: "veteran life insurance leads" isn\'t in the title.']
    assert "title ✗" in report.line() and "URL ✓" in report.line()


def test_no_keyword_and_no_links_are_both_said():
    report = seo.check(keyword="", title="t", slug="s", h1="h", html="<p>x</p>",
                       description="d", alt="a", base=BASE, removed=["x"])

    assert report.line() == "No target keyword was chosen."
    assert len(report.warnings()) == 2


# ------------------------------------------------ what the model is given


def test_the_model_is_told_which_posts_it_may_link_to():
    from wilbyte.copywriter import build_user_message
    from wilbyte.models import Transcript, Video

    said = build_user_message(
        Video(video_id="x", title="t", url="https://youtu.be/x"),
        Transcript(video_id="x", text="words", source="manual"),
        [("Aged vs Fresh Leads", "https://agentleadlab.com/post/aged-vs-fresh")],
    )
    none = build_user_message(
        Video(video_id="x", title="t", url="https://youtu.be/x"),
        Transcript(video_id="x", text="words", source="manual"),
    )

    assert "- Aged vs Fresh Leads — https://agentleadlab.com/post/aged-vs-fresh" in said
    assert "no others" in said
    assert "add no links to agentleadlab.com/post/" in none


def test_the_keyword_fields_are_read_back(config):
    from wilbyte.copywriter import parse_copy_package

    got = parse_copy_package({
        "article_h1": "Veteran Life Insurance Leads: Why They Convert",
        "article_html": "<h1>x</h1><p>y</p>",
        "headline_options": ["Veteran Life Insurance Leads Convert", "What Veteran Leads Know First"],
        "meta_title": "Veteran Life Insurance Leads", "meta_description": "d" * 120,
        "url_slug": "veteran-life-insurance-leads",
        "primary_keyword": "  Veteran Life Insurance Leads</",
        "secondary_keywords": ["VA leads", " ", "vet leads cost"],
        "cover_alt": "Cover image: why veteran life insurance leads convert " + "x" * 200,
    }, config)

    assert got.primary_keyword == "veteran life insurance leads"
    assert got.secondary_keywords == ["va leads", "vet leads cost"]
    assert got.cover_alt.startswith("Cover image: why veteran") and len(got.cover_alt) <= 125


# ------------------------------------------------ what there is to link to


def test_only_live_posts_are_offered_to_link_to(config):
    from wilbyte.bot import jobs

    posts = [
        {"urlSlug": "aged-vs-fresh", "title": "Aged vs Fresh", "status": "PUBLISHED"},
        {"urlSlug": "next-week", "title": "Next Week", "status": "SCHEDULED"},
        {"urlSlug": "draft", "title": "Draft", "status": "DRAFT"},
        {"urlSlug": "gone", "title": "Gone", "status": "PUBLISHED", "deleted": True},
    ]
    context = NS(blog_id="b", client=NS(list_posts=lambda blog: posts))

    assert jobs.link_targets(context, config) == [
        ("Aged vs Fresh", "https://agentleadlab.com/post/aged-vs-fresh")]


def test_without_ghl_what_ryte_published_himself_is_offered(config, tmp_path):
    from datetime import datetime, timezone

    from wilbyte.bot import jobs
    from wilbyte.state import Ledger

    ledger = Ledger(path=tmp_path / "l.json")
    now = datetime.now(timezone.utc)
    ledger.record(video_id="a", title="Live One", url_slug="live-one", scheduled_at=now,
                  ghl_post_id="p", published_at=now)
    ledger.record(video_id="b", title="Not Yet", url_slug="not-yet", scheduled_at=now, ghl_post_id="q")

    def refuses(blog):
        raise RuntimeError("GHL down")

    assert jobs.link_targets(NS(blog_id="b", client=NS(list_posts=refuses)), config, ledger) == [
        ("Live One", "https://agentleadlab.com/post/live-one")]
