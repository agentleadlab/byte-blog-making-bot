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
