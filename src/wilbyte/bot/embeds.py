"""Discord embed builders."""

from __future__ import annotations

from datetime import datetime

import discord

from ..models import BlogPost, Video
from ..pipeline import format_slot

GREEN = 0x35D07F
GREY = 0x4F5660
RED = 0xE0525F
AMBER = 0xE0A32E


def _truncate(text: str, limit: int) -> str:
    """Discord rejects oversized field values, so clip defensively."""
    text = text or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def post_preview(post: BlogPost, *, index: int, total: int, mode: str) -> discord.Embed:
    """The review card: everything that is about to be sent to GHL."""
    embed = discord.Embed(
        title=_truncate(post.title, 256),
        description=_truncate(post.description, 4096),
        colour=AMBER if mode != "dry-run" else GREY,
    )
    embed.set_author(name=f"Post {index} of {total} · {mode}")
    embed.add_field(name="URL slug", value=f"`{post.url_slug}`", inline=False)
    embed.add_field(
        name="Scheduled",
        value=format_slot(post.scheduled_at) if post.scheduled_at else "not scheduled",
        inline=True,
    )
    embed.add_field(name="Words", value=str(post.copy.word_count or "?"), inline=True)
    embed.add_field(name="Category", value=post.category, inline=True)
    embed.add_field(name="Author", value=post.author, inline=True)
    embed.add_field(name="Cover alt", value=f"`{post.cover_alt_text}`", inline=True)
    embed.add_field(
        name="Article H1 (not used as the title)",
        value=_truncate(post.copy.article_h1, 1024),
        inline=False,
    )
    embed.add_field(name="Source", value=post.video.short_url, inline=False)

    if post.warnings:
        embed.add_field(
            name="⚠ Worth a look",
            value=_truncate("\n".join(f"• {w}" for w in post.warnings), 1024),
            inline=False,
        )

    embed.set_image(url="attachment://cover.png")
    embed.set_footer(text=post.canonical_link)
    return embed


def plan_summary(
    pairs: list[tuple[Video, datetime]], *, skipped: int, source: str
) -> discord.Embed:
    embed = discord.Embed(
        title="Posting plan",
        description=f"{len(pairs)} post(s) queued from {source}.",
        colour=GREEN,
    )
    if skipped:
        embed.description += f"\n{skipped} video(s) already processed — skipped."

    lines = []
    for video, slot in pairs:
        lines.append(f"**{format_slot(slot)}**\n{_truncate(video.title, 90)}\n{video.short_url}")

    body = "\n\n".join(lines) or "Nothing pending."
    # Embed descriptions cap at 4096; split the long list across fields instead.
    for chunk_index, start in enumerate(range(0, len(lines), 5)):
        chunk = "\n\n".join(lines[start : start + 5])
        embed.add_field(
            name="​" if chunk_index else "Queue",
            value=_truncate(chunk, 1024),
            inline=False,
        )
    if not lines:
        embed.add_field(name="Queue", value=body, inline=False)
    return embed


def result_summary(
    *, created: int, skipped: int, failed: int, mode: str, output_dir: str
) -> discord.Embed:
    colour = RED if failed else GREEN
    embed = discord.Embed(title="Run complete", colour=colour)
    embed.add_field(name="Created", value=str(created), inline=True)
    embed.add_field(name="Skipped", value=str(skipped), inline=True)
    embed.add_field(name="Failed", value=str(failed), inline=True)
    embed.add_field(name="Mode", value=mode, inline=False)
    embed.set_footer(text=f"Files: {output_dir}")
    return embed


def status_summary(
    *, processed: int, recent: list[str], next_slots: list[datetime], booked_days: int | None
) -> discord.Embed:
    embed = discord.Embed(title="Byte status", colour=GREEN)
    embed.add_field(name="Posts in ledger", value=str(processed), inline=True)
    embed.add_field(
        name="Days booked in GHL",
        value=str(booked_days) if booked_days is not None else "—",
        inline=True,
    )
    if next_slots:
        embed.add_field(
            name="Next open slots",
            value="\n".join(format_slot(s) for s in next_slots),
            inline=False,
        )
    if recent:
        embed.add_field(name="Most recent", value=_truncate("\n".join(recent), 1024), inline=False)
    return embed


def error(message: str) -> discord.Embed:
    return discord.Embed(title="Something went wrong", description=_truncate(message, 4000), colour=RED)
