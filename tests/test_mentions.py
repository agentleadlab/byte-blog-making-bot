"""Parsing @mentions into pipeline actions."""

import pytest

from wilbyte.bot.mentions import HELP_TEXT, parse

BOT = "<@1234567890>"
PLAYLIST = "https://youtube.com/playlist?list=PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0"
VIDEO = "https://youtu.be/w7mazKut2lk"


def test_a_bare_link_means_run_one_post():
    request = parse(f"{BOT} {PLAYLIST}")

    assert request.action == "run"
    assert request.source == PLAYLIST
    assert request.limit == 1
    assert request.mode == "scheduled"
    assert request.force is False


@pytest.mark.parametrize(
    "text",
    [
        "{bot} {url} 3",
        "{bot} 3 {url}",
        "{bot} {url} 3 posts",
        "{bot} next 3 from {url}",
        "{bot} {url} x3",
        "{bot} do the next 3 videos {url} please",
    ],
)
def test_counts_are_read_in_any_arrangement(text):
    request = parse(text.format(bot=BOT, url=PLAYLIST))

    assert request.action == "run"
    assert request.limit == 3


def test_digits_inside_the_url_are_not_mistaken_for_a_count():
    """The playlist id contains '41' and '8' - neither is the batch size."""
    request = parse(f"{BOT} {PLAYLIST}")

    assert request.limit == 1


def test_limit_is_capped_to_max_batch():
    request = parse(f"{BOT} {PLAYLIST} 99", max_batch=10)

    assert request.limit == 10


@pytest.mark.parametrize(
    "word,expected",
    [("draft", "draft"), ("drafts", "draft"), ("dry", "preview"), ("test", "preview")],
)
def test_mode_words(word, expected):
    request = parse(f"{BOT} {word} {VIDEO}")

    assert request.mode == expected


def test_default_mode_is_scheduled():
    assert parse(f"{BOT} {VIDEO}").mode == "scheduled"


@pytest.mark.parametrize("word", ["force", "again", "redo", "rerun"])
def test_force_words(word):
    assert parse(f"{BOT} {word} {VIDEO}").force is True


def test_plan_keyword():
    request = parse(f"{BOT} plan {PLAYLIST}")

    assert request.action == "plan"
    assert request.source == PLAYLIST


@pytest.mark.parametrize("word", ["status", "ledger", "state"])
def test_status_keywords_need_no_link(word):
    request = parse(f"{BOT} {word}")

    assert request.action == "status"
    assert request.source is None


@pytest.mark.parametrize("text", ["", "hey", "hello there", "what can you do", "help"])
def test_a_mention_with_no_link_asks_for_help(text):
    assert parse(f"{BOT} {text}").action == "help"


def test_bare_playlist_id_is_accepted():
    request = parse(f"{BOT} PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0 2")

    assert request.action == "run"
    assert request.source == "PLry8Oc9d41ocnWVvVOmhxPLVUtlUmliQ0"
    assert request.limit == 2


def test_trailing_punctuation_is_stripped_from_the_link():
    request = parse(f"{BOT} can you do {VIDEO}, thanks")

    assert request.source == VIDEO


def test_role_mentions_are_ignored():
    request = parse(f"{BOT} <@&999> {VIDEO}")

    assert request.action == "run"
    assert request.source == VIDEO


def test_nickname_style_mention_is_stripped():
    request = parse(f"<@!1234567890> {VIDEO}")

    assert request.source == VIDEO


# ----------------------------------------------------------------------- cover


def test_cover_split_on_a_pipe():
    request = parse(f"{BOT} cover Aged, Fresh, Premium | Why Agents Stall")

    assert request.action == "cover"
    assert request.kicker == "Aged, Fresh, Premium"
    assert request.headline == "Why Agents Stall"


def test_cover_split_on_the_last_colon():
    request = parse(f"{BOT} cover Lead Flow: Why Your Dead Leads Are Not Dead")

    assert request.kicker == "Lead Flow"
    assert request.headline == "Why Your Dead Leads Are Not Dead"


def test_cover_with_one_line_is_all_headline():
    request = parse(f"{BOT} cover Why Agents Stall")

    assert request.action == "cover"
    assert request.kicker is None
    assert request.headline == "Why Agents Stall"


def test_cover_wins_over_a_link_in_the_same_message():
    """'cover' is checked first so its free text isn't eaten by URL parsing."""
    request = parse(f"{BOT} cover Aged, Fresh | Premium Leads")

    assert request.action == "cover"


def test_help_text_lists_the_real_commands():
    for fragment in ("status", "plan", "cover", "draft", "preview", "Schedule it"):
        assert fragment in HELP_TEXT
