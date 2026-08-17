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
    # Alt text mirrors the slug, matching the manual process.
    assert post.cover_alt_text == post.url_slug
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
