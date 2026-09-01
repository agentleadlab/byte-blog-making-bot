"""What Agent Lead Lab sells, and reading an order for it."""

import pytest

from wilbyte import products


@pytest.mark.parametrize(
    "typed,wanted",
    [
        ("40 basic spanish leads", "40 Basic Spanish Leads"),
        ("25 otp iul leads", "25 OTP IUL Leads"),
        ("15 TEXT VERIFIED VET", "15 Text Verified VET"),
        ("30 blue collar iul", "30 Blue Collar IUL"),
        ("text-verified widow leads", "Text-Verified Widow Leads"),
        ("spanish instant iul leads", "Spanish Instant IUL Leads"),
    ],
)
def test_the_line_is_title_case_however_it_was_typed(typed, wanted):
    """A client reads this. It should look written, not transcribed - and the
    acronyms are the team's, so they keep their case."""
    assert products.titled(typed) == wanted


def test_the_money_is_read_off_the_message():
    assert products.amount_asked("Can you make me a link for $621 for 40 leads") == 621.0
    assert products.amount_asked("$1,250.50 for 80 vets") == 1250.50
    assert products.amount_asked("40 basic spanish leads") is None


def test_the_quantity_is_the_number_that_is_not_the_money():
    said = "a Klarna link for $621 for 40 basic Spanish Leads"
    assert products.how_many(said) == 40
    assert products.amount_asked(said) == 621.0


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("$621 for 40 basic Spanish Leads", "Spanish Instant IUL Leads"),
        ("25 text verified vet leads", "Text-Verified Veteran Leads"),
        ("30 blue collar", "Text-Verified Blue Collar Leads"),
        ("40 trucker iul leads", "Text-Verified Trucker IUL Leads"),
        ("15 aged fex", "Text-Verified Aged Final Expense Leads"),
        ("20 mortgage protection leads", "Text-Verified Mortgage Protection Leads"),
        ("50 facebook iul", "Facebook IUL Leads"),
        ("10 otp vet standard", "OTP Vet Standard"),
        ("35 spanish text verified iul", "Spanish Text-Verified IUL Leads"),
        ("12 widows", "Text-Verified Widow Leads"),
    ],
)
def test_which_package_was_asked_for(said, wanted):
    found = products.find(said)
    assert found is not None and found.name == wanted


def test_aged_is_never_read_as_fresh():
    """Two different products at two different prices."""
    assert products.find("15 aged veteran leads").name == (
        "Text-Verified Aged Veteran Leads"
    )
    assert products.find("15 veteran leads").name == "Text-Verified Veteran Leads"


def test_spanish_iul_is_not_plain_iul():
    """Every word of an alias has to be there. Filing one as the other is a
    wrong invoice in a client's inbox."""
    assert products.find("40 spanish instant iul").name == "Spanish Instant IUL Leads"
    # "IUL" alone is four of these packages - Facebook, Text-Verified, Spanish,
    # aged - so it names none of them on its own. A question, not a guess.
    assert products.find("40 iul leads") is None


def test_a_package_nobody_can_place_comes_back_empty():
    """Better a question than an invoice for the wrong thing."""
    assert products.find("40 something we don't sell") is None
    assert products.find("") is None


def test_every_package_has_its_own_description():
    named = [product.name for product in products.CATALOGUE]
    assert len(named) == len(set(named))
    assert all(product.description.strip() for product in products.CATALOGUE)


def test_the_line_a_client_sees_carries_the_count():
    found = products.find("$621 for 40 basic Spanish Leads")
    assert products.line_for("$621 for 40 basic Spanish Leads", found) == (
        "40 Spanish Instant IUL Leads"
    )
