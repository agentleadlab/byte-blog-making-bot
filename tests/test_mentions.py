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


@pytest.mark.parametrize("text", ["start Aug 18", "from monday", "resume 2026-08-18"])
def test_a_leading_start_word_moves_the_calendar(text):
    request = parse(f"{BOT} {text}")

    assert request.action == "start"
    assert request.brief and request.brief.strip()


@pytest.mark.parametrize(
    "text",
    [
        "sms about getting a fresh start this quarter",
        "email from the team about aged leads",
        "ad for agents starting from scratch",
    ],
)
def test_start_words_inside_a_brief_do_not_move_the_calendar(text):
    """These are ordinary words in copy. Only the opening word is the command."""
    assert parse(f"{BOT} {text}").action == "write"


@pytest.mark.parametrize("word", ["fields", "raw", "inspect"])
def test_fields_keywords_need_no_link(word):
    request = parse(f"{BOT} {word}")

    assert request.action == "fields"
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
    for fragment in ("status", "plan", "cover", "draft", "preview", "Schedule it",
                     "sms", "email", "learn", "corpus"):
        assert fragment in HELP_TEXT


# ------------------------------------------------------------------- copywriting


@pytest.mark.parametrize(
    "text,expected",
    [
        ("sms about the price drop", "sms"),
        ("write me an email about aged leads", "email"),
        ("make an ad for stuck agents", "ad"),
        ("draft a script hook on lead costs", "script"),
        ("landing page for the OTP offer", "landing"),
        ("fb copy about live transfers", "ad"),
        ("vsl for the new funnel", "script"),
    ],
)
def test_format_words_route_to_the_writer(text, expected):
    request = parse(f"{BOT} {text}")

    assert request.action == "write"
    assert request.format_key == expected


@pytest.mark.parametrize(
    "text,expected_brief",
    [
        ("sms about the price drop", "the price drop"),
        ("write me an email about aged leads", "aged leads"),
        ("email: we're raising prices", "we're raising prices"),
        ("ad for agents stuck at 20 leads a week", "agents stuck at 20 leads a week"),
    ],
)
def test_the_brief_survives_without_the_format_word(text, expected_brief):
    assert parse(f"{BOT} {text}").brief == expected_brief


def test_a_format_word_beats_a_youtube_link():
    """'email about the new video <link>' wants an email, not a blog post."""
    request = parse(f"{BOT} email about the new video {VIDEO}")

    assert request.action == "write"
    assert request.format_key == "email"


def test_posts_does_not_trigger_the_social_format():
    """'3 posts' is a batch size for the blog pipeline, not a social post."""
    request = parse(f"{BOT} {PLAYLIST} 3 posts")

    assert request.action == "run"
    assert request.limit == 3


@pytest.mark.parametrize("word", ["learn", "train", "remember", "study"])
def test_learn_keywords(word):
    request = parse(f"{BOT} {word}")

    assert request.action == "learn"
    assert request.format_key is None


def test_learn_can_carry_a_label():
    assert parse(f"{BOT} learn sms").format_key == "sms"
    assert parse(f"{BOT} learn blog").format_key == "blog"


@pytest.mark.parametrize("word", ["corpus", "library", "memory", "knowledge"])
def test_corpus_keywords(word):
    assert parse(f"{BOT} {word}").action == "corpus"


@pytest.mark.parametrize("word", ["check", "diagnose", "doctor", "checkup"])
def test_check_keywords(word):
    assert parse(f"{BOT} {word}").action == "check"


def test_check_picks_up_a_link_to_test_youtube_with():
    request = parse(f"{BOT} check {PLAYLIST}")

    assert request.action == "check"
    assert request.source == PLAYLIST


def test_test_still_means_preview_mode_not_a_system_check():
    """'test' was a mode word before the check command existed - keep it that way."""
    request = parse(f"{BOT} test {VIDEO}")

    assert request.action == "run"
    assert request.mode == "preview"


def test_a_format_word_with_no_brief_still_routes_to_write():
    """The handler asks what it should be about rather than guessing."""
    request = parse(f"{BOT} sms")

    assert request.action == "write"
    assert request.brief == ""


# ------------------------------------------------------------ the calendar


@pytest.mark.parametrize("word", ["schedule", "scheduled", "calendar", "upcoming"])
def test_calendar_words_ask_for_the_schedule(word):
    assert parse(f"<@999> {word}").action == "schedule"


def test_status_is_still_its_own_thing():
    """Different questions: what's booked vs what's actually going out."""
    assert parse("<@999> status").action == "status"


def test_a_link_with_a_count_still_runs():
    assert parse("<@999> https://youtu.be/w7mazKut2lk 3").action == "run"


# ----------------------------------------------------------- several links at once

VID_A = "https://youtu.be/aaaaaaaaaaa"
VID_B = "https://youtu.be/bbbbbbbbbbb"
VID_C = "https://youtu.be/ccccccccccc"


def test_every_link_in_the_message_is_picked_up():
    """A week of posts arrives as pasted links, not a playlist."""
    request = parse(f"{BOT} {VID_A} {VID_B} {VID_C}")

    assert request.action == "run"
    assert request.sources == (VID_A, VID_B, VID_C)


def test_the_count_defaults_to_the_number_of_links():
    """Pasting twelve links and getting one post back would be absurd."""
    assert parse(f"{BOT} {VID_A} {VID_B} {VID_C}").limit == 3


def test_an_explicit_count_still_wins():
    assert parse(f"{BOT} {VID_A} {VID_B} {VID_C} 2").limit == 2


def test_links_on_separate_lines_are_all_found():
    assert len(parse(f"{BOT}\n{VID_A}\n{VID_B}\n{VID_C}").sources) == 3


def test_a_link_pasted_twice_makes_one_post():
    request = parse(f"{BOT} {VID_A} {VID_B} {VID_A}")

    assert request.sources == (VID_A, VID_B)
    assert request.limit == 2


def test_the_batch_cap_still_applies():
    many = " ".join(f"https://youtu.be/vid{i:07d}xxx" for i in range(20))

    assert parse(f"{BOT} {many}", max_batch=10).limit == 10


def test_source_is_still_the_first_link():
    """plan and check act on one source; they must keep working."""
    assert parse(f"{BOT} {VID_A} {VID_B}").source == VID_A


# ------------------------------------------------- a call link needs no verb

ZOOM_REC = "https://us06web.zoom.us/rec/share/qVQ0NLoGrnVQQZbM.VeLdfj6Y"
FATHOM_REC = "https://fathom.video/share/abc123xyz"


@pytest.mark.parametrize("link", [ZOOM_REC, FATHOM_REC, "https://fathom.video/calls/12345"])
def test_a_bare_call_link_files_the_recording(link):
    """It can't mean anything else, and getting the help text back is a trap."""
    assert parse(f"{BOT} {link}").action == "recording"


def test_the_passcode_line_comes_along_with_it():
    request = parse(f"{BOT} {ZOOM_REC}\nPasscode: U^M^s7Bw")

    assert request.action == "recording"
    assert "U^M^s7Bw" in request.brief


def test_a_youtube_link_still_means_write_a_blog_post():
    """This is why `recording` exists as a word at all."""
    assert parse(f"{BOT} {VIDEO}").action == "run"


def test_fathom_video_in_a_url_is_not_a_request_for_a_script():
    """`fathom.video` contains "video", an alias for the script format."""
    assert parse(f"{BOT} {FATHOM_REC}").action == "recording"


def test_a_format_word_inside_any_link_is_not_a_format_word():
    """A blog URL with "ad" in it was enough to trigger the ad writer."""
    request = parse(f"{BOT} plan https://agentleadlab.com/blog/ad-spend-tracker {PLAYLIST}")

    assert request.action == "plan"


def test_a_typed_format_word_still_wins():
    assert parse(f"{BOT} email about https://agentleadlab.com/x").action == "write"


# ------------------------------------------- what RYTE can actually read


@pytest.mark.parametrize("word", ["calls", "zoom", "visible"])
def test_asking_what_recordings_are_visible(word):
    assert parse(f"{BOT} {word}").action == "calls"


def test_a_zoom_link_is_still_a_recording_to_file():
    """`zoom.us` contains "zoom", which is now also a command word."""
    assert parse(f"{BOT} {ZOOM_REC}\nPasscode: U^M^s7Bw").action == "recording"


def test_a_command_word_inside_a_url_is_not_a_command():
    """A link is an address. Its insides are not instructions."""
    assert parse(f"{BOT} https://agentleadlab.com/blog/lead-status-check").action == "help"


def test_a_typed_command_word_still_wins_over_a_link():
    assert parse(f"{BOT} status https://agentleadlab.com/blog/x").action == "status"


def test_calls_can_carry_a_link_to_diagnose():
    request = parse(f"{BOT} calls {ZOOM_REC}")

    assert request.action == "calls"
    assert request.brief == ZOOM_REC


def test_calls_on_its_own_carries_no_link():
    assert parse(f"{BOT} calls").brief == ""


# ------------------------------------- asking for a recording back again

# Filing one and asking for one are the same words apart from a link. The
# gallery exists to be asked, and a link somebody has to dig out by hand is
# most of the way back to not having filed it.


@pytest.mark.parametrize(
    "text",
    [
        "need the sales recording for Derrick Robison call",
        "where is the recording for Arlene",
        "send me the notion link for Derrick",
        "can you find the sales call with Mayra",
        "show me the recordings",
    ],
)
def test_asking_for_a_card_is_a_lookup(text):
    assert parse(f"{BOT} {text}").action == "findcall"


@pytest.mark.parametrize(
    "text",
    [
        "{bot} recording {link}",
        "{bot} {link}",
        "{bot} Sales: Derrick Robison {link}",
        "{bot} need this recording filed {link}",
    ],
)
def test_handing_one_over_still_files_it(text):
    """The same words with a link mean the opposite thing."""
    assert parse(text.format(bot=BOT, link=ZOOM_REC)).action == "recording"


def test_a_lookup_needs_both_halves():
    """"send me the link" alone could be about anything at all."""
    assert parse(f"{BOT} send me the link").action != "findcall"
    assert parse(f"{BOT} recording").action != "findcall"


@pytest.mark.parametrize(
    "text",
    [
        "need the video of Derrick Robison",
        "send me the video for Arlene",
        "where's the video of the Mayra call",
    ],
)
def test_asking_for_the_video_is_a_lookup(text):
    """"video" is an alias for the script format, so this came back as a written
    script about Derrick Robison rather than his recording."""
    assert parse(f"{BOT} {text}").action == "findcall"


@pytest.mark.parametrize(
    "text",
    [
        "write a script about the new video",
        "make a vsl for aged leads",
        "I want a hook for the video",
        "script for the video on lead costs",
    ],
)
def test_asking_for_copy_about_a_video_still_writes(text):
    assert parse(f"{BOT} {text}").action == "write"


# --------------------------------------------- the daily Trello board


@pytest.mark.parametrize("word", ["board", "trello"])
def test_board_words_need_no_link(word):
    request = parse(f"{BOT} {word}")

    assert request.action == "board"
    assert request.source is None


@pytest.mark.parametrize("word", ["rollover", "carryover"])
def test_rollover_words_are_their_own_thing(word):
    """Different questions: what's on the board vs what tonight would move."""
    assert parse(f"{BOT} {word}").action == "rollover"


def test_a_board_word_inside_a_link_is_not_the_command():
    assert parse(f"{BOT} https://trello.com/b/abc/board").action != "board"
