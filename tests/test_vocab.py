"""Teaching RYTE a lead-type word, instead of shipping him one.

In one evening the cards carried "PHX STNDRD", "PHX 2.0", "Ascend" and "Index
Universal Life" — four spellings of products RYTE already knew, and four code
changes. The vocabulary belongs to whoever sells the leads.
"""

from __future__ import annotations

import pytest

from wilbyte import agents, vocab


def test_a_word_is_learned_as_what_it_means():
    held = vocab.teach("STNDRD", "standard")

    assert held["stndrd"] == {"word": "STNDRD", "means": "standard", "kind": "tier"}


def test_the_word_reads_back_the_way_it_was_typed():
    """It goes into a list somebody asked for, so "PHX STNDRD" should look
    like what they wrote, even though matching ignores the case."""
    held = vocab.teach("PHX STNDRD", "standard")

    assert held["phx stndrd"]["word"] == "PHX STNDRD"


@pytest.mark.parametrize(
    "typed,settled",
    [
        ("Standard", "standard"), ("basic", "standard"),
        ("Plus", "plus"), ("OTP", "plus"), ("text verified", "plus"),
        ("IUL", "iul"), ("Index Universal Life", "iul"), ("veterans", "vet"),
        ("Phoenix", "phnx"), ("uprise", "phnx"),
        ("SIUL", "spanish"), ("BC", "blue collar"),
    ],
)
def test_what_it_means_is_read_the_way_somebody_would_type_it(typed, settled):
    assert vocab.settle(typed) == settled


def test_a_meaning_it_does_not_have_is_refused_with_the_list():
    with pytest.raises(vocab.VocabError) as raised:
        vocab.teach("PREM", "premium")

    assert "premium" in str(raised.value)
    assert "standard" in str(raised.value) and "iul" in str(raised.value)


def test_a_word_that_would_break_the_matching_is_refused():
    with pytest.raises(vocab.VocabError):
        vocab.teach("(.*)", "plus")


def test_a_word_can_be_forgotten():
    held = vocab.teach("PREM", "plus")

    assert vocab.forget("prem", held=held) == {}


def test_forgetting_something_never_taught_changes_nothing():
    held = vocab.teach("PREM", "plus")

    assert vocab.forget("nothing", held=held) == held


def test_teaching_the_same_word_twice_replaces_it():
    held = vocab.teach("PREM", "plus")
    held = vocab.teach("PREM", "standard", held=held)

    assert len(held) == 1
    assert held["prem"]["means"] == "standard"


# --------------------------------- and it changes how a card reads


def test_an_unknown_word_leaves_the_lead_type_unplaceable():
    have = ["Phoenix Standard", "Phoenix Plus"]

    assert agents.tier_of("PHNX PREM") is None
    assert agents.match_checklist("PHNX PREM", have, tier=None) is None


def test_the_taught_word_files_the_card():
    agents.taught(vocab.teach("PREM", "plus"))
    have = ["Phoenix Standard", "Phoenix Plus"]

    assert agents.tier_of("PHNX PREM") == "plus"
    assert agents.match_checklist("PHNX PREM", have, tier="plus") == "Phoenix Plus"


def test_a_taught_family_is_a_family():
    agents.taught(vocab.teach("Summit", "fex"))

    assert agents.family_of("Summit Standard") == "fex"
    assert agents.shape_of("Summit Standard") == agents.shape_of("Final Expense Basic")


def test_a_taught_qualifier_keeps_its_own_checklist():
    """A qualifier is what makes Spanish IUL a different campaign from IUL,
    so a taught one has to hold that apart too."""
    agents.taught(vocab.teach("LATAM", "spanish"))

    assert agents.shape_of("OTP LATAM IUL") == agents.shape_of("OTP Spanish IUL")
    assert agents.shape_of("OTP LATAM IUL") != agents.shape_of("OTP IUL")


def test_a_shipped_word_is_not_overruled_by_a_taught_one():
    """A card that already says IUL is not waiting on anything typed into
    Discord."""
    agents.taught(vocab.teach("IUL", "fex"))

    assert agents.family_of("OTP IUL Plus") == "iul"


def test_a_taught_word_matches_on_its_own_and_not_inside_another():
    agents.taught(vocab.teach("PREM", "plus"))

    assert agents.tier_of("PHNX PREM") == "plus"
    assert agents.tier_of("PREMIER VETS") is None


def test_forgetting_a_word_puts_the_card_back_where_it_was():
    held = vocab.teach("PREM", "plus")
    agents.taught(held)
    agents.taught(vocab.forget("PREM", held=held))

    assert agents.tier_of("PHNX PREM") is None


# --------------------------------- which word to ask about


@pytest.mark.parametrize(
    "label,unknown",
    [
        ("PHNX PREM", ["PREM"]),
        ("40 Summit Basic", ["Summit"]),
        # Everything here is already known, so there is nothing to ask.
        ("PHX STNDRD", []),
        ("25 OTP Spanish IUL", []),
        ("OTP Trucker IUL leads", []),
        # A price is not a word anybody forgot to teach.
        ("$1050/WEEK- UPRISE PHX PLUS", []),
        ("OTP VETS/FEX", []),
    ],
)
def test_the_word_worth_asking_about(label, unknown):
    assert agents.words_it_cannot_place(label) == unknown


def test_what_it_reads_from_disk_survives_a_restart(tmp_path):
    where = tmp_path / "lead-words.json"
    vocab.save(vocab.teach("PREM", "plus"), where)

    agents.taught(vocab.load(where))

    assert agents.tier_of("PHNX PREM") == "plus"


def test_a_file_somebody_broke_does_not_stop_it_starting(tmp_path):
    where = tmp_path / "lead-words.json"
    where.write_text("{not json at all", encoding="utf-8")

    assert vocab.load(where) == {}


def test_a_stored_word_whose_meaning_no_longer_exists_is_dropped(tmp_path):
    """A product renamed out of the code shouldn't leave a word pointing at
    nothing."""
    where = tmp_path / "lead-words.json"
    where.write_text(
        '{"prem": {"word": "PREM", "means": "gone", "kind": "tier"}}', encoding="utf-8"
    )

    assert vocab.load(where) == {}


def test_nothing_taught_yet_says_how_to_teach():
    assert "@RYTE words" in vocab.describe({})


def test_the_list_groups_them_by_what_they_are():
    held = vocab.teach("PREM", "plus")
    held = vocab.teach("Summit", "fex", held=held)

    said = vocab.describe(held)

    assert "**tier**" in said and "**family**" in said
    assert "`PREM` → plus" in said and "`Summit` → fex" in said
