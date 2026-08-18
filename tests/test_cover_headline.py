"""Keeping the cover headline short enough to read as a poster."""

import pytest

from wilbyte.models import CopyPackage, Headline
from wilbyte.selection import cover_headline, kicker_candidates


def package(h1, *options):
    return CopyPackage(
        article_h1=h1,
        article_html="<h1>x</h1>",
        headline_options=[Headline(o) for o in options],
        meta_title="t",
        meta_description="d",
        url_slug="s",
    )


def test_a_short_title_is_used_as_is(config):
    copy = package("H1", "Turned Your Leads Off?")

    assert cover_headline(copy, Headline("Turned Your Leads Off?"), config) == (
        "Turned Your Leads Off?"
    )


def test_a_trailing_parenthetical_is_dropped(config):
    title = Headline("Why Most Agents Never Move Up a Lead Tier (And Stay Stuck)")
    copy = package("H1", title.text)

    assert cover_headline(copy, title, config) == "Why Most Agents Never Move Up a Lead Tier"


def test_a_colon_clause_is_dropped(config):
    title = Headline("The Lead Progression Roadmap: Aged to Fresh to Premium Leads")
    copy = package("H1", title.text)

    assert cover_headline(copy, title, config) == "The Lead Progression Roadmap"


def test_a_whole_option_wins_over_a_cut_down_title(config):
    long_title = Headline("Here Is Exactly Why Your Sales Stalled After You Paused Ads")
    copy = package("H1", long_title.text, "Why Your Sales Stalled After You Paused")

    assert cover_headline(copy, long_title, config) == (
        "Why Your Sales Stalled After You Paused"
    )


def test_a_much_shorter_option_does_not_win(config):
    """Swapping a full line for a tiny one wastes the canvas."""
    long_title = Headline("Here Is Exactly Why Your Sales Stalled After You Paused Ads")
    copy = package("H1", long_title.text, "Dead Leads")

    assert cover_headline(copy, long_title, config) != "Dead Leads"


def test_the_longest_option_that_fits_is_preferred(config):
    """Use the space available rather than defaulting to the shortest line."""
    long_title = Headline("Here Is Exactly Why Your Sales Stalled After You Paused Ads")
    copy = package("H1", long_title.text, "Dead Leads", "Why Your Dead Leads Are Not Dead")

    assert cover_headline(copy, long_title, config) == "Why Your Dead Leads Are Not Dead"


def test_an_unbreakable_title_is_clipped_at_the_word_limit(config):
    title = Headline("Never Turn Your Lead Generation Off Because Momentum Is Everything")
    copy = package("H1", title.text)

    result = cover_headline(copy, title, config)

    assert len(result) <= config.cover.headline_max_chars
    assert title.text.startswith(result)


def test_a_clipped_headline_does_not_end_on_a_stopword(config):
    title = Headline("Stop Turning Your Leads Off And Start Working The Ones You Have")
    copy = package("H1", title.text)

    result = cover_headline(copy, title, config)

    assert result.split()[-1].lower() not in {"and", "the", "your", "of", "to"}


@pytest.mark.parametrize("punct", [".", ",", ":", " -", "—"])
def test_trailing_punctuation_is_stripped(config, punct):
    title = Headline(f"Lead Flow Is Cash Flow{punct}")
    copy = package("H1", title.text)

    assert cover_headline(copy, title, config) == "Lead Flow Is Cash Flow"


def test_a_headline_a_hair_over_the_limit_is_kept_whole(config):
    """Cutting one character over changed what the line said.

    "The Wrong Question New Life Insurance Agents Ask" is 47 characters against
    a 46 limit, and trimming produced "...New Life Insurance Agents" - which
    reads as a different, unfinished claim. The renderer scales to its box, so
    slightly over just means slightly smaller.
    """
    title = Headline(text="The Wrong Question New Life Insurance Agents Ask")
    copy = package(title.text)

    assert cover_headline(copy, title, config).endswith("ASK".title())


def test_a_genuinely_long_headline_is_still_cut(config):
    long_title = Headline(
        text="Here Is Exactly Why Your Agency Sales Stalled After You Paused Your Lead Flow"
    )
    copy = package(long_title.text)

    result = cover_headline(copy, long_title, config)

    assert len(result) <= config.cover.headline_max_chars * 1.25
    assert len(result) < len(long_title.text)


def test_a_kicker_does_not_end_on_a_joining_word(config):
    """"What To Sell First As" reads as a sentence someone interrupted."""
    copy = package("some h1", "What To Sell First As A New Life Insurance Agent")

    candidates = kicker_candidates(copy, config)

    assert candidates
    assert not any(c.lower().split()[-1] in {"as", "the", "and", "of", "to"} for c in candidates)


# ------------------------------------------------- the kicker has to mean something


def cover_for(config, h1, options, written=""):
    from wilbyte.selection import choose_title, plan_cover

    copy = package(h1, *options)
    copy.cover_kicker = written
    title, _note = choose_title(copy)
    return plan_cover(copy, title, config)


def test_the_kicker_is_never_a_headline_cut_off_mid_thought(config):
    """The reported bug: "WHY YOUR LIFE INSURANCE INTRO" above an unrelated line.

    It came from taking the first five words of a headline, which is the first
    five words of a sentence and reads as one someone interrupted.
    """
    plan = cover_for(
        config,
        "Aged or Fresh Leads? Answer One Question First",
        [
            "Why Your Life Insurance Intro Script Is Costing You Deals",
            "Aged or Fresh Leads? Answer One Question First",
            "The Intro Script That Decides Your Whole Call",
        ],
    )

    assert plan.kicker == "LIFE INSURANCE INTRO SCRIPT"


def test_a_kicker_written_for_the_post_is_used(config):
    """The copywriter has read the article; a word-overlap rule hasn't."""
    plan = cover_for(
        config,
        "Aged or Fresh Leads? Answer One Question First",
        [
            "Why Your Life Insurance Intro Script Is Costing You Deals",
            "Aged or Fresh Leads? Answer One Question First",
            "The Intro Script That Decides Your Whole Call",
        ],
        written="Your Intro Script",
    )

    assert plan.kicker == "YOUR INTRO SCRIPT"
    assert plan.source_note == "kicker written for this post"


def test_a_written_kicker_that_just_repeats_the_headline_is_refused(config):
    """It sits directly above the headline; saying it twice is worse than none."""
    plan = cover_for(
        config,
        "Stop Guessing Which Leads To Buy",
        ["Stop Guessing Which Leads To Buy", "The Lead Type Playbook For New Agents"],
        written="Stop Guessing Which Leads",
    )

    assert plan.kicker != "STOP GUESSING WHICH LEADS"


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Why Your Life Insurance Intro Script Is Costing You Deals",
         "Life Insurance Intro Script"),
        ("The Intro Script That Decides Your Whole Call", "Intro Script"),
        ("Why Do New Agents Stall After Thirty Days?", "New Agents Stall"),
        ("What To Sell First As A New Life Insurance Agent", "New Life Insurance Agent"),
    ],
)
def test_a_headline_reduces_to_the_phrase_that_names_its_subject(config, headline, expected):
    """Cut where the sentence stops naming and starts commenting - not at a word count."""
    from wilbyte.selection import label_from

    assert label_from(headline, config) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Why Your Life Insurance Intro",   # opens a question it never finishes
        "The Intro Script That Decides",   # trails off on a verb wanting an object
        "New Agents Stall After Thirty",   # one word short of the word that ends it
        "How To Sell More",                # opens a clause
        "Leads",                           # one word is a category, not a kicker
    ],
)
def test_fragments_are_rejected_as_kickers(config, text):
    from wilbyte.selection import reads_as_a_label

    assert not reads_as_a_label(text)


@pytest.mark.parametrize(
    "text",
    ["Life Insurance Intro Script", "New Agents Stall", "Aged Vs Fresh Leads", "Stop Guessing"],
)
def test_real_phrases_are_accepted_as_kickers(config, text):
    from wilbyte.selection import reads_as_a_label

    assert reads_as_a_label(text)


def test_every_kicker_from_a_batch_of_real_headlines_stands_alone(config):
    """A sweep, because this failed in production on a post that looked fine."""
    from wilbyte.selection import reads_as_a_label

    posts = [
        ("Aged or Fresh Leads? Answer One Question First",
         ["Why Your Life Insurance Intro Script Is Costing You Deals",
          "Aged or Fresh Leads? Answer One Question First",
          "The Intro Script That Decides Your Whole Call"]),
        ("I Lost $100,000 in Under a Month. Here's What Rebuilt It.",
         ["5 Lessons From Losing $100K in My Agency",
          "How I Rebuilt To $660K a Month After Losing Everything",
          "What Failure Taught Me About Team, Focus and Routine"]),
        ("Why Do New Agents Stall?",
         ["Why Do New Agents Stall After Thirty Days?",
          "How Do You Know If The Leads Are Bad?",
          "What Should A New Agent Buy First?"]),
        ("Fresh vs Aged Insurance Leads: When to Buy Each",
         ["Why New Agents Should Only Work Aged Leads",
          "Fresh vs Aged Insurance Leads: When to Buy Each",
          "The Working Style Each Lead Type Demands"]),
    ]

    for h1, options in posts:
        plan = cover_for(config, h1, options)
        assert reads_as_a_label(plan.kicker), f"{plan.kicker!r} from {h1!r}"
