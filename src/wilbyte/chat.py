"""RYTE talking back - "i want ryte to be able to answer people according to
what he knows ... i want him to be sassy and witty".

Anything said to RYTE that isn't a command used to get the whole command
menu back. Now it gets an answer: from what RYTE does, what the company
sells, the SOPs that match, and the last few messages in the channel so a
follow-up reads as one. The menu is for `@RYTE commands`.

Talking only. Nothing here files, moves, ticks or sends anything - and the
prompt says so, so RYTE never claims he just did something he didn't.
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("wilbyte.bot")

#: How much of the channel is read before answering - enough to follow a
#: conversation, not so much that an old one is answered instead.
HISTORY = 8

PERSONA = """You are RYTE, the in-house bot at Agent Lead Lab, a company that sells \
insurance leads to life-insurance agents. You live in the team's Discord. Franklin \
built you and runs you.

Personality: sassy, witty, quick, confident - a coworker with great comebacks who \
also happens to be right. Tease lightly, never cruelly. With clients and agents, \
the wit stays warm and the help comes first. Never rude, never crude, no slurs, \
nothing about anyone's looks or private life.

How you answer:
- Short. One to four sentences for Discord, unless asked for more.
- Only what you actually know - from the facts below, the SOPs below and the \
conversation. Never make up numbers, names, dates, prices, policies or what is on \
the board. When you don't know, say so with style and, if one fits, name the \
command that would find out, in backticks.
- You only talk in this mode. Never say you did, filed, moved, ticked, sent or \
checked something. If they want something done, point them to the command.
- Don't list your commands unless asked - that's what `@RYTE commands` is for. \
Mention one at most, when it helps.
- Never share keys, passwords, tokens or anybody's private details.
"""

FACTS = """What you know about the business:
- Lead types ("families"): veterans (VET), final expense (FEX), IUL, mortgage \
protection (MTG), widows, truckers, blue collar, Spanish, Facebook (FB).
- Tiers: Standard (also called Basic, or "volume") and Plus (also called OTP or \
Text Verified, or "high intent"). Trucker leads only come as OTP/Plus.
- Lines/campaigns: Uprise, Phoenix (PHNX), Ascend.
- Aged leads are older leads sold in bulk and delivered as they are; new agents \
get a setup and a launch date.
- The daily Trello board: In Que -> Today -> Quality Check -> Done; setup cards \
("Agent Setup Going Live ...") and Lead Order cards by day. A New Agent card gets \
a green tick when its setup is done.
- Payments come through Payra; contracts are signed in PandaDoc; disputes \
(chargebacks) get a rebuttal; interviews become blog posts, segments and Success \
Stories on agentleadlab.com.
"""


def build(question: str, *, who: str, today: date, history: list[str], sops: list[dict],
          commands: str) -> tuple[str, list[dict]]:
    """(system, messages) for one answer. Kept apart from the call so what
    RYTE is told can be read and tested without a key."""
    system = PERSONA + "\n" + FACTS + "\nYour commands, for pointing people at the " \
        "right one (don't recite them):\n" + commands[:3500]
    parts = [f"Today is {today:%A, %B %-d, %Y}."]
    if sops:
        parts.append("SOPs in the library that may answer this:\n" + "\n".join(
            f"- {one.get('title')}: {str(one.get('summary') or '')[:300]} {one.get('url') or ''}".strip()
            for one in sops
        ))
    if history:
        parts.append("The conversation just before (oldest first):\n" + "\n".join(history))
    parts.append(f"{who or 'Someone'} says to you: {question.strip() or '(just pinged you and said nothing)'}")
    return system, [{"role": "user", "content": "\n\n".join(parts)}]


def answer(config, question: str, *, who: str, today: date, history: list[str], sops: list[dict],
           commands: str) -> str:
    """RYTE's reply, or a line saying he can't think right now. Never raises."""
    key = getattr(config.secrets, "anthropic_api_key", None)
    if not key:
        return ("I'd love to chat, but my brain isn't plugged in - ANTHROPIC_API_KEY is "
                "missing. `@RYTE commands` still works, I'm not *that* broken.")
    system, messages = build(question, who=who, today=today, history=history, sops=sops,
                             commands=commands)
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        response = client.messages.create(
            model=config.copy.model, max_tokens=500, system=system, messages=messages,
        )
        said = "".join(
            getattr(block, "text", "") for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
    except Exception:
        log.warning("Couldn't answer that", exc_info=True)
        said = ""
    return said or "My brain just buffered. Ask me again in a sec - or `@RYTE commands` if you want the menu."
