import pytest

from wilbyte.copywriter import (
    CopywriterError,
    build_user_message,
    load_system_prompt,
    normalize_slug,
    parse_copy_package,
)
from wilbyte.models import Transcript, Video


def payload(**overrides):
    base = {
        "article_h1": "The Lead Progression Roadmap",
        "article_html": "<h1>The Lead Progression Roadmap</h1><p>Body copy here.</p>",
        "headline_options": [
            "The Lead Progression Roadmap: Aged to Fresh to Premium",
            "Aged, Fresh, Premium: The Roadmap to $40K Months",
            "Why Most Agents Never Move Up a Lead Tier (And Stay Stuck)",
        ],
        "meta_title": "Insurance Lead Progression: Aged to Fresh to Premium",
        "meta_description": "Aged leads buy skill. Fresh leads buy speed. Premium leads buy time.",
        "url_slug": "/Insurance Lead-Progression--Roadmap/",
    }
    base.update(overrides)
    return base


def test_slug_is_normalized(config):
    package = parse_copy_package(payload(), config)

    assert package.url_slug == "insurance-lead-progression-roadmap"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/cost-per-booked-appointment", "cost-per-booked-appointment"),
        ("Why Agents Quit", "why-agents-quit"),
        ("aged--leads___work", "aged-leads-work"),
    ],
)
def test_normalize_slug(raw, expected):
    assert normalize_slug(raw) == expected


def test_empty_slug_is_rejected():
    with pytest.raises(CopywriterError):
        normalize_slug("///")


def test_overlong_meta_fields_are_trimmed_on_a_word_boundary(config):
    long_title = "Insurance Lead Progression Roadmap For Agents Who Want Consistent Monthly Growth"
    package = parse_copy_package(payload(meta_title=long_title), config)

    assert len(package.meta_title) <= config.copy.meta_title_max
    assert not package.meta_title.endswith(" ")
    # Trimmed at a space, so the last word is intact.
    assert long_title.startswith(package.meta_title)


def test_missing_required_field_raises(config):
    broken = payload()
    del broken["meta_description"]

    with pytest.raises(CopywriterError, match="meta_description"):
        parse_copy_package(broken, config)


def test_single_headline_option_is_rejected(config):
    """With one option there is no way to pick a title that differs from the H1."""
    with pytest.raises(CopywriterError, match="headline options"):
        parse_copy_package(payload(headline_options=["Only One"]), config)


def test_word_count_is_estimated_from_html_when_absent(config):
    package = parse_copy_package(payload(), config)

    assert package.word_count == 7  # "The Lead Progression Roadmap" + "Body copy here."


def test_user_message_carries_the_transcript_and_short_link():
    video = Video(video_id="w7mazKut2lk", title="From $0 to $40K Months", url="https://x")
    transcript = Transcript(video_id="w7mazKut2lk", text="there is a blueprint", source="youtube")

    message = build_user_message(video, transcript)

    assert "https://youtu.be/w7mazKut2lk" in message
    assert "there is a blueprint" in message


def test_system_prompt_file_exists_and_is_substantial():
    prompt = load_system_prompt()

    assert "Agent Lead Lab" in prompt
    assert len(prompt) > 500
