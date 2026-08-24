"""Videos announced before YouTube had finished captioning them.

The announcement bot posts the moment a video goes up, and the automatic
captions are not ready then. The run failed, the red box said "could not get a
transcript", and writing the post became something somebody had to remember -
which means it doesn't get written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wilbyte import waiting

LINK = "https://youtu.be/4U4M45jo_L0"
NOON = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def queue(tmp_path):
    return waiting.Queue(path=tmp_path / "waiting.json")


# --------------------------------------- telling "not yet" from "not ever"


@pytest.mark.parametrize(
    "problem",
    [
        "No caption track published for 4U4M45jo_L0.",
        "Could not retrieve a transcript for the video",
        "Data API: No caption track published | web: could not retrieve a transcript",
    ],
)
def test_captions_that_have_not_appeared_yet_are_worth_waiting_for(problem):
    assert waiting.not_ready_yet(problem) is True


@pytest.mark.parametrize(
    "problem",
    [
        "HTTP 403: permission denied on captions.download",
        "No caption track published — the video is private",
        "This video is members-only",
        "cookies.txt does not look like a Netscape format cookies file",
        "Could not build the cover image",
    ],
)
def test_a_door_that_is_shut_is_not_waited_on(problem):
    """A video whose captions are refused outright will still be refused in six
    hours, and trying twenty-four times is just noise."""
    assert waiting.not_ready_yet(problem) is False


# ------------------------------------------------------------- the queue


def test_a_video_goes_on_the_list_and_comes_back_off(tmp_path):
    store = queue(tmp_path)

    store.add(LINK, title="Reduce Chargebacks", now=NOON)

    assert waiting.Queue.load(store.path).items[LINK].title == "Reduce Chargebacks"

    store.drop(LINK)
    assert waiting.Queue.load(store.path).items == {}


def test_the_list_survives_a_restart(tmp_path):
    """RYTE gets restarted rather more often than YouTube captions a video."""
    store = queue(tmp_path)
    store.add(LINK, title="Reduce Chargebacks", channel_id=123, now=NOON)

    reloaded = waiting.Queue.load(store.path)

    assert reloaded.items[LINK].channel_id == 123
    assert reloaded.items[LINK].first_seen == NOON.isoformat()


def test_asking_again_counts_the_try_rather_than_starting_over(tmp_path):
    store = queue(tmp_path)
    store.add(LINK, now=NOON)

    store.add(LINK, now=NOON + timedelta(minutes=20))

    assert store.items[LINK].tries == 2
    assert store.items[LINK].first_seen == NOON.isoformat(), "still as early as it was"


def test_a_video_just_tried_is_not_tried_again_immediately(tmp_path):
    store = queue(tmp_path)
    store.add(LINK, now=NOON)

    assert store.due(now=NOON + timedelta(minutes=5)) == []
    assert store.due(now=NOON + waiting.RETRY_EVERY + timedelta(seconds=1))


def test_the_oldest_wait_goes_first(tmp_path):
    store = queue(tmp_path)
    store.add("https://youtu.be/older", now=NOON)
    store.add("https://youtu.be/newer", now=NOON + timedelta(minutes=1))

    due = store.due(now=NOON + timedelta(hours=1))

    assert [item.url for item in due] == ["https://youtu.be/older", "https://youtu.be/newer"]


def test_waiting_forever_is_not_waiting_it_is_a_loop(tmp_path):
    store = queue(tmp_path)
    store.add(LINK, now=NOON)
    later = NOON + waiting.GIVE_UP_AFTER

    assert store.due(now=later) == []
    assert [item.url for item in store.expired(now=later)] == [LINK]


def test_one_still_in_time_has_not_expired(tmp_path):
    store = queue(tmp_path)
    store.add(LINK, now=NOON)

    assert store.expired(now=NOON + timedelta(hours=1)) == []


def test_a_corrupt_file_is_an_empty_queue_rather_than_a_crash(tmp_path):
    """Losing the waiting list is a nuisance. Not starting is worse."""
    path = tmp_path / "waiting.json"
    path.write_text("{ not json at all")

    assert waiting.Queue.load(path).items == {}


def test_no_file_yet_is_simply_nothing_waiting(tmp_path):
    assert waiting.Queue.load(tmp_path / "nothing.json").items == {}


# ------------------------------------------------- the message people read


def test_terminal_colour_codes_are_taken_out_of_an_error():
    """yt-dlp writes them even when nothing is a terminal, and they arrive in
    Discord as "[0;31mERROR" in the middle of the sentence."""
    from wilbyte.bot.client import _readable

    said = _readable("web: \x1b[0;31mERROR:\x1b[0m could not retrieve a transcript")

    assert "\x1b" not in said
    assert "0;31m" not in said
    assert "could not retrieve a transcript" in said


def test_a_plain_error_is_left_as_it_is():
    from wilbyte.bot.client import _readable

    assert _readable("No caption track published for abc.") == (
        "No caption track published for abc."
    )


# ------------------------------------- the clock that must not keep resetting


def test_noting_another_try_does_not_restart_the_six_hours(tmp_path):
    """The retry loop takes a video off the list to run it. If a failed retry
    puts it back as new, the wait resets every fifteen minutes and it waits for
    ever."""
    store = queue(tmp_path)
    store.add(LINK, now=NOON)

    for minutes in range(15, 6 * 60, 15):
        store.add(LINK, now=NOON + timedelta(minutes=minutes))

    assert store.items[LINK].first_seen == NOON.isoformat()
    assert store.expired(now=NOON + waiting.GIVE_UP_AFTER)


def test_a_video_removed_and_re_added_starts_its_wait_again(tmp_path):
    """The other direction: a genuinely new announcement of the same link is a
    new wait, not a continuation of one that already gave up."""
    store = queue(tmp_path)
    store.add(LINK, now=NOON)
    store.drop(LINK)

    store.add(LINK, now=NOON + timedelta(hours=8))

    assert store.items[LINK].first_seen == (NOON + timedelta(hours=8)).isoformat()
    assert store.expired(now=NOON + timedelta(hours=9)) == []
