"""Move posts that are already scheduled onto different days.

Opening the weekend up doesn't help on its own: the posts are already sitting
on Monday, Tuesday, Wednesday, and the new Saturday and Sunday slots go by
empty while everything queues behind them. So the calendar has to be re-laid,
not just widened.

The rule is the one anybody would use by hand: keep the running order, and
give each post the earliest slot left. Nothing is reordered - the post that was
going out first still goes out first - it just goes out sooner.

Everything here is pure. It takes the posts and the slots and pairs them up, so
what a rearrange would do can be shown before a single date is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Move:
    """One post, where it sits now, and where it would go."""

    video_id: str
    title: str
    was: datetime | None
    now: datetime

    @property
    def moved(self) -> bool:
        """Whether this is actually a change. Most of a queue usually isn't."""
        if self.was is None:
            return True
        return self.was != self.now


def pair(posts: list[tuple[str, str, datetime | None]], slots: list[datetime]) -> list[Move]:
    """Give each post the next slot, in the order they were already going out.

    Extra posts with no slot left are dropped rather than guessed at: a queue
    longer than the calendar is a real problem and inventing a date hides it.
    """
    return [
        Move(video_id=video_id, title=title, was=was, now=slot)
        for (video_id, title, was), slot in zip(posts, slots)
    ]


def held_days(posts: list[tuple[str, str, datetime | None]], tz) -> set[date]:
    """The days these posts currently occupy.

    They have to come out of the taken set before new slots are worked out, or
    every post blocks its own move and the whole queue stays where it is.
    """
    return {when.astimezone(tz).date() for _, _, when in posts if when is not None}


def summarise(moves: list[Move], *, unmoved_note: bool = True) -> str:
    """What the rearrange would do, as something a person can check."""
    changing = [move for move in moves if move.moved]
    if not changing:
        return "Nothing to move — every post is already on the earliest day it can be."

    lines = []
    for move in changing:
        was = f"{move.was:%a %b %d}" if move.was else "no date"
        lines.append(f"• **{move.title}**\n  {was} → {move.now:%a %b %d} at {move.now:%-I:%M %p}")

    staying = len(moves) - len(changing)
    head = f"{len(changing)} post(s) would move:"
    if staying and unmoved_note:
        lines.append(f"\n{staying} already on the right day, left alone.")
    return f"{head}\n" + "\n".join(lines)


def explain_failures(problems: list[str]) -> str:
    """One readable account of what went wrong, not fifteen copies of it.

    When GHL refuses a whole queue it refuses it for one reason, and printing
    that reason once per post buries it - the message is too long to read, and
    the part that says *why* is the part that gets cut off the end.
    """
    if not problems:
        return ""
    if len(problems) == 1:
        return f"⚠ Couldn't move it:\n{problems[0]}"

    # Same reason for all of them? Say it once, and name what it happened to.
    reasons = {problem.split(" — ", 1)[-1] for problem in problems}
    if len(reasons) == 1:
        titles = ", ".join(problem.split(" — ", 1)[0] for problem in problems[:3])
        more = f" and {len(problems) - 3} more" if len(problems) > 3 else ""
        return (
            f"⚠ All {len(problems)} were refused for the same reason "
            f"({titles}{more}):\n{reasons.pop()}"
        )
    return "⚠ Couldn't move:\n" + "\n".join(f"• {problem}" for problem in problems)
