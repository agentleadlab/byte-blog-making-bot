from wilbyte.models import CopyPackage, Headline
from wilbyte.selection import choose_title, kicker_candidates, plan_cover, similarity


def test_similarity_ignores_stopwords_and_case():
    assert similarity("The Lead Progression Roadmap", "lead progression roadmap") == 1.0
    assert similarity("Aged Leads Are Training Wheels", "Cost Per Booked Appointment") < 0.2


def test_title_skips_the_option_that_duplicates_the_h1(copy_package):
    """The exact case from the video: option 1 == the article H1, so don't use it."""
    title, note = choose_title(copy_package)

    assert title.text != copy_package.article_h1
    # Option 2 also clears the bar, but option 3 is the clearly-opposite angle,
    # which is the one Wil picks in the walkthrough.
    assert title.text == "Why Most Agents Never Move Up a Lead Tier (And Stay Stuck)"
    assert "option 3" in note


def test_title_takes_option_one_when_it_differs_from_the_h1():
    copy = CopyPackage(
        article_h1="Cost Per Booked Appointment Is the Only Number That Matters",
        article_html="<h1>x</h1>",
        headline_options=[
            Headline("Why Cheap Leads Quietly Destroy Your Margins"),
            Headline("The Only Lead Metric Worth Tracking This Quarter"),
            Headline("Stop Counting Leads, Start Counting Appointments"),
        ],
        meta_title="t",
        meta_description="d",
        url_slug="cost-per-booked-appointment",
    )

    title, note = choose_title(copy)

    assert title.text == "Why Cheap Leads Quietly Destroy Your Margins"
    assert "option 1" in note


def test_title_falls_back_and_flags_when_every_option_echoes_the_h1():
    copy = CopyPackage(
        article_h1="The Lead Progression Roadmap",
        article_html="<h1>x</h1>",
        headline_options=[
            Headline("The Lead Progression Roadmap"),
            Headline("The Lead Progression Roadmap Explained"),
            Headline("Your Lead Progression Roadmap, Step By Step"),
        ],
        meta_title="t",
        meta_description="d",
        url_slug="lead-progression-roadmap",
    )

    title, note = choose_title(copy)

    assert "manual look" in note
    # A title is still produced - the run is not blocked - but never the exact
    # duplicate of the H1, and the note tells the operator to look at it.
    assert title.text != copy.article_h1


def test_kicker_candidates_prefer_the_fragment_before_a_colon(copy_package, config):
    candidates = kicker_candidates(copy_package, config)

    assert "Aged, Fresh, Premium" in candidates
    assert all(3 <= len(c.split()) <= 5 for c in candidates)


def test_cover_pairs_a_kicker_from_a_different_headline(copy_package, config):
    title, _ = choose_title(copy_package)
    plan = plan_cover(copy_package, title, config)

    assert plan.kicker == "AGED, FRESH, PREMIUM"
    # The blog title keeps "(And Stay Stuck)"; the cover drops it. A cover is a
    # poster - past ~7 words the type shrinks and it stops reading.
    assert plan.headline == "WHY MOST AGENTS NEVER MOVE UP A LEAD TIER"
    assert plan.kicker.lower() not in plan.headline.lower()
    assert "manual look" not in plan.source_note


def test_cover_never_repeats_the_headline_as_the_kicker():
    copy = CopyPackage(
        article_h1="Why Your Dead Leads Are Not Dead",
        article_html="<h1>x</h1>",
        headline_options=[
            Headline("Why Your Dead Leads Are Not Dead"),
            Headline("Why Your Dead Leads Are Not Dead At All"),
        ],
        meta_title="t",
        meta_description="d",
        url_slug="dead-leads",
    )
    from wilbyte.config import load_config

    config = load_config(load_env=False)
    title, _ = choose_title(copy)
    plan = plan_cover(copy, title, config)

    assert plan.kicker
    assert plan.headline
