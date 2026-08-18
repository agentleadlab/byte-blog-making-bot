import json
from types import SimpleNamespace

import pytest

from wilbyte import copywriter
from wilbyte.copywriter import (
    CopywriterError,
    as_markup,
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


# ------------------------------------------------------- the assembled brief


def test_reference_documents_are_appended_to_the_brief():
    """The brief is the operating instructions; the docs are the standard."""
    prompt = load_system_prompt()

    assert prompt.startswith("# Agent Lead Lab")
    for marker in (
        "Brand Facts Sheet",          # what is true
        "Cost Per Acquisition",       # the approved exemplar
        "BCD",                        # the hook system
        "Noteworthy Format",          # the content-promo format
    ):
        assert marker in prompt, marker


def test_the_operating_brief_comes_before_the_reference_material():
    """It says outright that it overrides them, so it has to be read first."""
    prompt = load_system_prompt()

    assert prompt.index("Hard rules") < prompt.index("Matthew Volkwyn")


def test_the_compliance_rules_survive_assembly():
    prompt = load_system_prompt()

    for rule in ("No income guarantees", "Clean language", "Invent nothing"):
        assert rule in prompt, rule


def test_a_missing_reference_folder_still_yields_the_brief(tmp_path):
    """Reference docs are additive - losing them must not break generation."""
    brief = tmp_path / "brief.md"
    brief.write_text("# Just the brief\n", encoding="utf-8")

    assert load_system_prompt(brief) == "# Just the brief\n"


# ------------------------------------------------------------ escaped markup


def test_escaped_html_from_the_model_is_decoded():
    """`&lt;h1&gt;` renders as a visible tag - it shipped that way once."""
    escaped = "&lt;h1&gt;How to Get Started&lt;/h1&gt; &lt;p&gt;Most agents...&lt;/p&gt;"

    assert as_markup(escaped) == "<h1>How to Get Started</h1> <p>Most agents...</p>"


def test_real_markup_is_left_alone():
    html = "<h1>Title</h1><p>Body &amp; more</p>"

    assert as_markup(html) == html


def test_an_ampersand_in_prose_survives_decoding():
    """Decoding twice would turn `&amp;lt;` in the copy into a real tag."""
    escaped = "&lt;p&gt;Aged &amp;amp; fresh leads&lt;/p&gt;"

    assert as_markup(escaped) == "<p>Aged &amp; fresh leads</p>"


def test_plain_text_with_no_markup_is_returned_as_is():
    assert as_markup("  Just a sentence.  ") == "Just a sentence."


def test_the_package_decodes_on_the_way_through(config):
    payload = {
        "article_h1": "H",
        "article_html": "&lt;h1&gt;H&lt;/h1&gt;&lt;p&gt;x&lt;/p&gt;",
        "headline_options": ["a" * 45, "b" * 45, "c" * 45],
        "meta_title": "t",
        "meta_description": "d" * 120,
        "url_slug": "s",
    }

    assert parse_copy_package(payload, config).article_html.startswith("<h1>")


# ------------------------------------------------- headline options that aren't a list


def test_a_json_string_of_options_is_decoded_not_iterated(config):
    """This shipped a post titled `[`.

    Asked for an array, the model returned a *string* that looks like one.
    Python iterates that by character, so three headlines became sixty single
    letters and the first one - an opening bracket - became the blog title.
    """
    package = copywriter.parse_copy_package(
        {
            "article_h1": "These Leads Changed — No. You Did.",
            "article_html": "<h1>x</h1>",
            "headline_options": json.dumps([
                "The 3-Question Test For Blaming Your Leads",
                "Your Leads Didn't Change. Your Follow-Up Did.",
                "Why Aged Leads Outproduce Fresh Ones",
            ]),
            "meta_title": "t",
            "meta_description": "d",
            "url_slug": "lead-quality-changed",
        },
        config,
    )

    assert len(package.headline_options) == 3
    assert package.headline_options[0].text.startswith("The 3-Question Test")


def test_a_newline_separated_string_of_options_is_split(config):
    package = copywriter.parse_copy_package(
        {
            "article_h1": "h",
            "article_html": "<h1>x</h1>",
            "headline_options": (
                "1. The 3-Question Test For Blaming Your Leads\n"
                "2. Your Leads Didn't Change. Your Follow-Up Did.\n"
            ),
            "meta_title": "t",
            "meta_description": "d",
            "url_slug": "s",
        },
        config,
    )

    assert [h.text for h in package.headline_options] == [
        "The 3-Question Test For Blaming Your Leads",
        "Your Leads Didn't Change. Your Follow-Up Did.",
    ]


def test_debris_short_enough_to_be_punctuation_is_refused(config):
    """Better a failed post than one titled with a stray bracket."""
    with pytest.raises(copywriter.CopywriterError, match="usable headline options"):
        copywriter.parse_copy_package(
            {
                "article_h1": "h",
                "article_html": "<h1>x</h1>",
                "headline_options": ["[", '"', "A"],
                "meta_title": "t",
                "meta_description": "d",
                "url_slug": "s",
            },
            config,
        )


def test_the_refusal_shows_what_the_model_actually_sent(config):
    """Otherwise this is unfixable from a Discord error message."""
    with pytest.raises(copywriter.CopywriterError, match="Raw value was"):
        copywriter.parse_copy_package(
            {
                "article_h1": "h",
                "article_html": "<h1>x</h1>",
                "headline_options": "[",
                "meta_title": "t",
                "meta_description": "d",
                "url_slug": "s",
            },
            config,
        )


def test_a_truncated_list_of_options_is_salvaged(config):
    """The real failure: the response stopped mid-headline, so JSON wouldn't parse.

    Two complete headlines are still two complete headlines. Throwing the post
    away over a missing bracket costs a full regeneration.
    """
    package = copywriter.parse_copy_package(
        {
            "article_h1": "h",
            "article_html": "<h1>x</h1>",
            "headline_options": (
                '["No Why, No Buy: Write Bigger Life Insurance Premium",'
                '"Why Your Clients Keep Picking the Cheapest Option",'
                '"The 10-Minute Drill That Kills Baby Premiums'
            ),
            "meta_title": "t",
            "meta_description": "d",
            "url_slug": "s",
        },
        config,
    )

    assert [h.text for h in package.headline_options] == [
        "No Why, No Buy: Write Bigger Life Insurance Premium",
        "Why Your Clients Keep Picking the Cheapest Option",
        "The 10-Minute Drill That Kills Baby Premiums",
    ]


def test_a_complete_list_gains_no_phantom_final_option(config):
    """The salvage must not read the closing bracket as a fourth headline."""
    package = copywriter.parse_copy_package(
        {
            "article_h1": "h",
            "article_html": "<h1>x</h1>",
            # Trailing comma: valid-looking, but json.loads refuses it, so this
            # takes the salvage path with a list that *did* finish.
            "headline_options": '["The First Real Headline","The Second Real Headline",]',
            "meta_title": "t",
            "meta_description": "d",
            "url_slug": "s",
        },
        config,
    )

    assert [h.text for h in package.headline_options] == [
        "The First Real Headline",
        "The Second Real Headline",
    ]


def test_running_out_of_room_is_reported_as_itself():
    """Otherwise it surfaces as a confusing parse error three steps later."""
    response = SimpleNamespace(stop_reason="max_tokens", content=[])

    with pytest.raises(copywriter.CopywriterError, match="ran out of room"):
        copywriter._extract_tool_input(response)
