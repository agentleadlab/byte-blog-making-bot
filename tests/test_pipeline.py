from datetime import datetime
from zoneinfo import ZoneInfo

from wilbyte.models import Video
from wilbyte.pipeline import assemble_post, select_pending_videos
from wilbyte.state import Ledger
from wilbyte.youtube import _clean_transcript, extract_playlist_id, extract_video_id


def video(vid="w7mazKut2lk"):
    return Video(video_id=vid, title="From $0 to $40K Months", url=f"https://youtu.be/{vid}")


def test_assemble_post_fills_every_ghl_field(copy_package, config, tmp_path):
    post = assemble_post(video(), copy_package, config, output_dir=tmp_path, report=lambda _: None)

    assert post.title == "Why Most Agents Never Move Up a Lead Tier (And Stay Stuck)"
    assert post.title != copy_package.article_h1
    assert post.url_slug == "insurance-lead-progression-roadmap"
    assert post.canonical_link == (
        "https://agentleadlab.com/post/insurance-lead-progression-roadmap"
    )
    assert post.category == "LeadLab"
    assert post.author == 'Arnold "Tre" Tarpley'
    assert "agentleadlab" in post.keywords
    assert "final expense leads" in post.keywords
    assert len(post.keywords) == 13
    # No alt sentence written for this one: the title, never the bare slug.
    assert post.cover_alt_text == post.title
    assert post.description == copy_package.meta_description


def test_assemble_post_writes_reviewable_artifacts(copy_package, config, tmp_path):
    assemble_post(video(), copy_package, config, output_dir=tmp_path, report=lambda _: None)
    post_dir = tmp_path / "insurance-lead-progression-roadmap"

    assert (post_dir / "post.html").exists()
    assert (post_dir / "copy.json").exists()
    assert (post_dir / "cover.png").exists()

    fields = (post_dir / "ghl-fields.txt").read_text()
    assert "URL slug:         insurance-lead-progression-roadmap" in fields
    assert "LeadLab" in fields
    assert "https://youtu.be/w7mazKut2lk" in fields


def test_ledger_skips_videos_already_processed(tmp_path):
    ledger = Ledger.load(tmp_path / "ledger.json")
    ledger.record(
        video_id="done1", title="t", url_slug="s",
        scheduled_at=datetime(2026, 8, 12, 10, tzinfo=ZoneInfo("America/New_York")),
        ghl_post_id="p1",
    )
    ledger.save()

    reloaded = Ledger.load(tmp_path / "ledger.json")
    pending, done = select_pending_videos([video("done1"), video("new1")], reloaded, limit=None)

    assert [v.video_id for v in pending] == ["new1"]
    assert [v.video_id for v in done] == ["done1"]


def test_ledger_limit_caps_the_batch(tmp_path):
    ledger = Ledger.load(tmp_path / "ledger.json")
    videos = [video(f"vid{i:08d}00") for i in range(5)]

    pending, _ = select_pending_videos(videos, ledger, limit=2)

    assert len(pending) == 2


def test_corrupt_ledger_does_not_block_a_run(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json")

    assert Ledger.load(path).entries == {}


def test_playlist_and_video_id_extraction():
    assert extract_playlist_id(
        "https://www.youtube.com/watch?v=w7mazKut2lk&list=PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0&index=8"
    ) == "PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0"
    assert extract_video_id("https://youtu.be/w7mazKut2lk?si=-FmMMfILQXxO8MvD") == "w7mazKut2lk"
    assert extract_video_id("https://www.youtube.com/watch?v=w7mazKut2lk") == "w7mazKut2lk"


def test_transcript_cleaning_strips_captions_and_timestamps():
    raw = "0:42\n[Music] there is a blueprint   for hitting\n1:15\n$40,000 a month [Applause]"

    assert _clean_transcript(raw) == "there is a blueprint for hitting $40,000 a month"


def test_a_body_with_no_tags_is_flagged_before_it_publishes(copy_package, config, tmp_path):
    """It shipped once as visible markup down the page; cheap to catch."""
    copy_package.article_html = "How to Get Started. Most agents start by asking..."
    video = Video(video_id="w7mazKut2lk", title="t", url="u")

    post = assemble_post(video, copy_package, config, output_dir=tmp_path, report=lambda _: None)

    assert any("no HTML tags" in w for w in post.warnings)


def test_a_normal_body_is_not_flagged(copy_package, config, tmp_path):
    video = Video(video_id="w7mazKut2lk", title="t", url="u")

    post = assemble_post(video, copy_package, config, output_dir=tmp_path, report=lambda _: None)

    assert not any("no HTML tags" in w for w in post.warnings)



# ------------------------------------------------------------------- SEO


def _seo_copy(copy_package, **changes):
    from dataclasses import replace

    return replace(copy_package, **changes)


def test_the_title_carries_the_keyword_it_is_written_for(copy_package, config, tmp_path):
    """The title went out as the headline *least* like the keyword-bearing H1:
    "The Lead Type That Tells You Why They Called" over "Veteran Life
    Insurance Leads: Why They Convert". It is still unlike the H1 - among the
    options that carry the keyword."""
    from wilbyte.models import Headline

    copy = _seo_copy(
        copy_package,
        primary_keyword="veteran life insurance leads",
        article_h1="Veteran Life Insurance Leads: Why They Convert (And Who They're Wrong For)",
        headline_options=[
            Headline(text="Veteran Life Insurance Leads: Why They Convert So Well"),
            Headline(text="The Lead Type That Tells You Why They Called"),
            Headline(text="What Veteran Life Insurance Leads Know Before You Dial"),
        ],
    )

    post = assemble_post(video(), copy, config, output_dir=tmp_path, report=lambda _: None)

    assert post.title == "What Veteran Life Insurance Leads Know Before You Dial"


def test_no_headline_with_the_keyword_falls_back_to_the_meta_title(copy_package, config, tmp_path):
    copy = _seo_copy(copy_package, primary_keyword="aged life insurance leads",
                     meta_title="Aged Life Insurance Leads: When to Graduate")

    post = assemble_post(video(), copy, config, output_dir=tmp_path, report=lambda _: None)

    assert post.title == "Aged Life Insurance Leads: When to Graduate"


def test_the_alt_text_is_the_sentence_written_for_it(copy_package, config, tmp_path):
    copy = _seo_copy(copy_package, cover_alt="Cover for a guide to moving up from aged to fresh insurance leads")

    post = assemble_post(video(), copy, config, output_dir=tmp_path, report=lambda _: None)

    assert post.cover_alt_text == "Cover for a guide to moving up from aged to fresh insurance leads"


def test_the_posts_own_keywords_are_its_first_tags(copy_package, config, tmp_path):
    copy = _seo_copy(copy_package, primary_keyword="insurance lead tiers",
                     secondary_keywords=["aged vs fresh leads", "Final Expense Leads"])

    post = assemble_post(video(), copy, config, output_dir=tmp_path, report=lambda _: None)

    assert post.keywords[:2] == ["insurance lead tiers", "aged vs fresh leads"]
    assert post.keywords.count("final expense leads") == 1
    assert "agentleadlab" in post.keywords


def test_the_review_card_says_where_the_keyword_landed(copy_package, config, tmp_path):
    copy = _seo_copy(copy_package, primary_keyword="insurance lead progression")

    post = assemble_post(video(), copy, config, output_dir=tmp_path, report=lambda _: None)

    assert post.seo_line.startswith("**insurance lead progression** — title ")
    assert "URL ✓" in post.seo_line
    assert any(one.startswith("SEO:") for one in post.warnings)


def test_links_to_posts_that_dont_exist_come_out_before_it_goes_out(copy_package, config, tmp_path, monkeypatch):
    from wilbyte import copywriter, pipeline

    body = (
        '<h1>Lead tiers</h1><p>See <a href="https://agentleadlab.com/post/real-post">the real one</a> '
        'and <a href="https://agentleadlab.com/post/made-up">an imagined one</a> and '
        '<a href="https://agentleadlab.com/schedule-a-call">book a call</a>.</p>'
    )
    monkeypatch.setattr(copywriter, "generate_copy",
                        lambda *a, **kw: _seo_copy(copy_package, article_html=body))

    post = pipeline.build_post(
        video(), NS_transcript(), config, output_dir=tmp_path, report=lambda _: None,
        related=[("Real", "https://agentleadlab.com/post/real-post")],
    )

    assert 'href="https://agentleadlab.com/post/real-post"' in post.copy.article_html
    assert "made-up" not in post.copy.article_html and "an imagined one" in post.copy.article_html
    assert "schedule-a-call" in post.copy.article_html
    assert any("don't exist" in one for one in post.warnings)


def NS_transcript():
    from wilbyte.models import Transcript

    return Transcript(video_id="w7mazKut2lk", text="words " * 50, source="manual")
