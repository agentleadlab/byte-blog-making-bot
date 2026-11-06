"""Each payment's card, a Discord message in a channel of its own.

"we can do discord card sure but i want specific channels only where thats
the only thing that will be there". One message per payment, kept up to
date in place as Rook, Nova, the hub and the sheet move:

    #agent-setup   new agents, renewals, overflow - the setup QC ("i want it
                   here"), and aged orders too until they have their own
    #aged-setup    aged orders - Nova's pulls - once that channel's made

Nothing else is said in those channels. A card is edited where it is; one
with a problem it didn't have before is sent again at the bottom with
Franklin tagged, so it's seen and not found by scrolling. Reads only:
nothing here writes to Trello, the hub or anywhere else.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .state import _state_dir

#: Where each card is: {"setup:<payment post id>": {"channel_id", "message_id", ...}}.
PATH = _state_dir() / "payment-cards.json"

#: The channels the cards live in, by kind - in Ryte The Goat. Each name is
#: read as its letters, so "👤│agent-setup" is agent-setup. The first that's
#: there; none are made.
CHANNELS = {"setup": ("agent-setup", "setup-cards"),
            "aged": ("aged-setup", "aged-cards", "aged-leads", "agent-setup", "setup-cards")}

#: A card's colour by where it stands.
COLOURS = {"problem": 0xE74C3C, "todo": 0xF1C40F, "waiting": 0x95A5A6, "ok": 0x2ECC71, "cant": 0x9B59B6}

#: A card's first line by where it stands.
STANDING = {"problem": "⚠️ Needs a look", "todo": "🟡 Matches - still something for the team",
            "waiting": "⏳ Waiting on Rook & Nova", "ok": "✅ Set up - matches the payment",
            "cant": "🃏 Rook/Nova can't set it up - the card's ours"}


def recalled() -> dict:
    try:
        held = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def remember(kind: str, key: str, **fields) -> dict:
    held = recalled()
    one = held.setdefault(f"{kind}:{key}", {})
    one.update(fields, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(held, indent=1))
    return one


def where(kind: str, key: str) -> dict:
    return recalled().get(f"{kind}:{key}") or {}


def setup_card(result, *, amount: str = "", link: str = "", waiting: bool = False) -> tuple[str, str, str]:
    """(standing, title, body) of a setup QC's card."""
    from .rookqc import said

    if result.cant:
        standing = "cant"
    elif waiting:
        standing = "waiting"
    elif result.problems:
        standing = "problem"
    elif result.todo:
        standing = "todo"
    else:
        standing = "ok"
    if getattr(result, "trello", "") and getattr(result, "hub", None) is None and standing in ("todo", "ok"):
        # Its Trello card made, not on the hub yet: the team's, not set up.
        standing, first = "cant", "🃏 With the team on Trello - not on the hub yet"
    elif getattr(result, "overflow", False) and standing in ("todo", "ok"):
        first = "🚚 Overflow - the team delivers it" + (" (delivered)" if standing == "ok" else "")
    else:
        first = STANDING[standing]
    if waiting:
        # Not flagged yet - Rook and Nova have their fifteen minutes.
        body = [first, "-# Checked again as they answer; flagged after fifteen minutes if it still doesn't match."]
        body += [f"-# ✓ {one}" for one in result.ok] + [f"-# {one}" for one in result.notes]
        if link:
            body.append(f"-# <{link}>")
    else:
        body = [f"**{first}**", *said(result, link=link).split("\n")[1:]]
    title = f"{result.client or 'A payment'}" + (f" - ${amount}" if amount else "")
    return standing, title, "\n".join(body)


def aged_card(result, *, link: str = "", waiting: str = "") -> tuple[str, str, str]:
    """(standing, title, body) of an aged pull's card - `waiting`: what was
    paid for, when Nova hasn't pulled it yet."""
    from .agedqc import said

    title = f"{result.client or 'An aged order'}" + (f" ({result.invoice})" if result.invoice else "")
    if waiting:
        body = ["**⏳ Waiting on Nova's pull**", waiting] + ([f"-# <{link}>"] if link else [])
        return "waiting", title, "\n".join(body)
    waiting = list(getattr(result, "waiting", None) or [])
    standing = "problem" if result.problems else "todo" if waiting else "ok"
    first = ("⚠️ Doesn't match" if result.problems else "🟡 Short - waiting on the team's answer" if waiting
             else f"✅ Pulled - matches {result.against}")
    body = [f"**{first}**", *(f"• {one}" for one in waiting), *said(result).split("\n")[1:]] \
        + ([f"-# <{link}>"] if link else [])
    return standing, title, "\n".join(body)
