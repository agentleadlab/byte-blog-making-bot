"""Keeping the cover headline short enough to read as a poster."""

import pytest

from wilbyte.models import CopyPackage, Headline
from wilbyte.selection import cover_headline


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
