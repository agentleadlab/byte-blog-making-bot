"""Orders in Discord, made into Trello cards when Franklin drops the gavel.

"instead of ryte looking out for the New Agent Card and Aged Leads card, it
will now create it instead". A payment and its notes are posted in Discord -
the order channel is made fresh every day under the same name, so the old
setups are flushed out. Nothing happens while the team is still adding notes.
When the order is ready, Franklin reacts with :leadlab_hammertime: and RYTE
makes the card, so the board's automations run from there:

- aged leads: made straight away, in Aged Leads Order Done;
- a fresh agent: shown first in Ryte The Goat, made on the Create button, in
  the Queue.

Each card is a copy of an example card - its checklists, labels and members
- with the title and description written in that card's own format from the
order. Notes added in the thread afterwards update the card. RYTE says
nothing in the Growth server for any of this.
"""

from __future__ import annotations

import re

#: The gavel. A custom emoji from Agent Lead Lab Clients - matched by name, so
#: it works from whichever server it's used in.
GAVEL = "leadlab_hammertime"

#: The example cards each kind is copied from, until told otherwise with
#: "@Ryte new agent template <link>" / "@Ryte aged template <link>".
DEFAULT_TEMPLATES = {"fresh": "https://trello.com/c/Hl06MPly", "aged": ""}

#: Where each kind goes, by list name - letters only, so "📥 In Que" and
#: "Queue" are both the Queue.
FRESH_LISTS = ("queue", "inqueue", "inque", "que")
AGED_LISTS = ("agedleadsorderdone",)

KINDS = {"fresh": "New Agent", "aged": "Aged Leads"}

_CARD_LINK = re.compile(r"trello\.com/c/([A-Za-z0-9]{6,})")


def is_gavel(emoji) -> bool:
    name = str(getattr(emoji, "name", None) or emoji or "")
    return name.strip(":").casefold() == GAVEL


def card_id_of(link: str) -> str:
    """"https://trello.com/c/Hl06MPly/123-new-agent" -> "Hl06MPly"."""
    found = _CARD_LINK.search(str(link or ""))
    return found.group(1) if found else ""


def may_gavel(person_id, config) -> bool:
    """Franklin, or whoever ORDER_GAVEL_USER_IDS names. A stray reaction from
    anybody else makes nothing - an aged card is made straight off it."""
    return str(person_id) in gavellers(config)


def gavellers(config) -> list[str]:
    """The Discord ids whose gavel counts - digits only, so "<@123>" in .env
    is 123."""
    named = re.findall(r"\d+", str(getattr(config.secrets, "order_gavel_user_ids", "") or ""))
    if not named:
        named = re.findall(r"\d+", str(getattr(config.secrets, "discord_notify_user_id", "") or ""))
    return named


def letters(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name or "").casefold())


def list_for(kind: str, lists: list[dict], *, wanted: str = "") -> dict | None:
    """The board list a kind of card goes in. `wanted` (ORDER_FRESH_LIST /
    ORDER_AGED_LIST) wins when it's set."""
    names = (letters(wanted),) if wanted else (FRESH_LISTS if kind == "fresh" else AGED_LISTS)
    for one in lists:
        if not one.get("closed") and letters(one.get("name", "")) in names:
            return one
    return None


def changed_lines(before: str, after: str) -> list[str]:
    """The description's lines that are new or different - for a comment when
    somebody has edited the card by hand and RYTE mustn't write over them."""
    had = {" ".join(line.split()) for line in str(before or "").splitlines()}
    return [line for line in str(after or "").splitlines()
            if line.strip() and " ".join(line.split()) not in had]


def same_text(one: str, two: str) -> bool:
    return " ".join(str(one or "").split()) == " ".join(str(two or "").split())


# ------------------------------------------------------------- remembered

PATH = None
TEMPLATES_PATH = None


def _where(name: str, override):
    from .state import _state_dir

    return override or (_state_dir() / name)


def _load(where) -> dict:
    import json

    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _save(where, held: dict) -> None:
    import json

    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1, default=str), encoding="utf-8")
    spare.replace(where)


def load() -> dict:
    """{gavelled message id: the order and its card}."""
    return _load(_where("order-cards.json", PATH))


def save(held: dict) -> None:
    _save(_where("order-cards.json", PATH), held)


def templates() -> dict:
    """{"fresh": link, "aged": link} - what's been set, over the defaults."""
    held = _load(_where("order-templates.json", TEMPLATES_PATH))
    return {kind: str(held.get(kind) or DEFAULT_TEMPLATES[kind]) for kind in DEFAULT_TEMPLATES}


def set_template(kind: str, link: str) -> None:
    where = _where("order-templates.json", TEMPLATES_PATH)
    held = _load(where)
    held[kind] = link
    _save(where, held)
