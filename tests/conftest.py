import pytest

from wilbyte.config import load_config
from wilbyte.models import CopyPackage, Headline


@pytest.fixture
def config():
    # load_env=False so a developer's real .env never leaks into a test run.
    return load_config(load_env=False)


@pytest.fixture
def copy_package():
    """Modelled on the real output shown in the walkthrough video."""
    return CopyPackage(
        article_h1="The Lead Progression Roadmap: Aged to Fresh to Premium",
        article_html=(
            "<h1>The Lead Progression Roadmap: Aged to Fresh to Premium</h1>"
            "<p>Most agents never move up a lead tier.</p>"
            "<h2>Stage 1: Aged Leads</h2><p>Aged leads exist for one reason: cheap reps.</p>"
        ),
        headline_options=[
            Headline("The Lead Progression Roadmap: Aged to Fresh to Premium"),
            Headline("Aged, Fresh, Premium: The Roadmap to $40K Months"),
            Headline("Why Most Agents Never Move Up a Lead Tier (And Stay Stuck)"),
        ],
        meta_title="Insurance Lead Progression: Aged to Fresh to Premium",
        meta_description=(
            "Aged leads buy skill. Fresh leads buy speed. Premium leads buy time. "
            "See the three-stage lead progression that moves agents from beginner to $40K months."
        ),
        url_slug="insurance-lead-progression-roadmap",
    )


@pytest.fixture(autouse=True)
def _shipped_words_only():
    """Every test reads the shipped vocabulary unless it says otherwise.

    The learned words live in module state so a lead type can be read without
    twenty call sites knowing the vocabulary is extensible — which means one
    test teaching a word would otherwise change what the next one reads.
    """
    from wilbyte import agents

    agents.taught({})
    yield
    agents.taught({})
