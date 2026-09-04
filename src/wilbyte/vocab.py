"""Words for lead types that RYTE was taught rather than shipped with.

Every product on this board gets written half a dozen ways. In one evening the
cards carried "PHX STNDRD" for Phoenix Standard, "PHX 2.0" for Phoenix Plus,
"Ascend" for the IUL line and "Index Universal Life" spelled out in full — and
each one cost a code change, because the vocabulary lived in the source.

That is the wrong place for it. The vocabulary is Franklin's, it changes when
the products get renamed, and nobody should need a developer to add a word. So
a word learned here is stored beside RYTE's other memory and read on every
start.

What can be learned is deliberately narrow: a word means a *family*, a *tier*,
or a *qualifier*, and nothing else. Those are the three things a lead type
reduces to, so a word mapped to one of them slots straight into the matching
that already exists. Anything larger — "an agency named on the card is not the
order" — is a rule about how a card is read rather than a word, and rules stay
in code where they can be tested.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .state import _state_dir

VOCAB_PATH = _state_dir() / "lead-words.json"


class VocabError(ValueError):
    """Raised when a word can't be taught as asked."""


# What a word is allowed to mean. The same three parts a lead type reduces to,
# named the way `agents.shape_of` names them so a learned word is
# indistinguishable from a shipped one once it is loaded.
FAMILIES = ("iul", "fex", "mtg", "vet", "widows", "phnx")
TIERS = ("standard", "plus")
QUALIFIERS = ("spanish", "blue collar", "trucker")

# Spellings somebody is likely to type for the same meaning.
_ALSO = {
    "iuls": "iul", "index universal life": "iul", "indexed universal life": "iul",
    "final expense": "fex", "fexs": "fex",
    "mortgage": "mtg",
    "vets": "vet", "veteran": "vet", "veterans": "vet",
    "widow": "widows",
    "phoenix": "phnx", "uprise": "phnx", "phx": "phnx",
    "basic": "standard", "standards": "standard", "basics": "standard",
    "otp": "plus", "text verified": "plus",
    "bc": "blue collar", "bluecollar": "blue collar",
    "siul": "spanish",
    "truckers": "trucker",
}


def kind_of(means: str) -> str | None:
    """"family", "tier", "qualifier" — or None when it means nothing here."""
    said = settle(means)
    if said in FAMILIES:
        return "family"
    if said in TIERS:
        return "tier"
    if said in QUALIFIERS:
        return "qualifier"
    return None


def settle(means: str) -> str:
    """The canonical name for what somebody typed on the right of the "="."""
    said = " ".join((means or "").split()).casefold()
    return _ALSO.get(said, said)


def choices() -> str:
    """Everything a word may be taught to mean, for when it was taught rubbish."""
    return (
        f"families: {', '.join(FAMILIES)}\n"
        f"tiers: {', '.join(TIERS)}\n"
        f"qualifiers: {', '.join(QUALIFIERS)}"
    )


# A word worth learning is one that could appear in a lead type: letters,
# digits, dots and spaces. Not punctuation that would change what the pattern
# built from it matches.
_WORD = re.compile(r"^[\w.&/+ -]{1,40}$")


def tidy(word: str) -> str:
    """The word as it will be stored and matched: spacing settled, case kept.

    Case is kept because it is what somebody will see in the list they asked
    for — "PHX STNDRD" reads back the way they typed it — and matching ignores
    it anyway.
    """
    said = " ".join((word or "").split())
    if not said:
        raise VocabError("Give me a word to learn, like `STNDRD = standard`.")
    if not _WORD.match(said):
        raise VocabError(
            f"“{said}” has punctuation I can't match on. Letters, digits, "
            "dots and spaces."
        )
    return said


def teach(word: str, means: str, *, held: dict | None = None) -> dict:
    """`held` with `word` added. Raises VocabError when it can't be taught."""
    said = tidy(word)
    kind = kind_of(means)
    if kind is None:
        raise VocabError(
            f"I don't know what “{' '.join((means or '').split())}” is. "
            f"A word can mean one of these:\n{choices()}"
        )
    return {**(held or {}), said.casefold(): {
        "word": said, "means": settle(means), "kind": kind,
    }}


def load(path: Path | None = None) -> dict:
    """Everything RYTE has been taught. {lowered word: {word, means, kind}}."""
    where = path or VOCAB_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A file somebody edited by hand into something unreadable is not worth
        # refusing to start over. The words come back by being taught again.
        return {}
    if not isinstance(data, dict):
        return {}

    held = {}
    for key, said in data.items():
        if not isinstance(said, dict):
            continue
        kind = kind_of(str(said.get("means") or ""))
        if kind is None:
            continue
        held[str(key).casefold()] = {
            "word": str(said.get("word") or key),
            "means": settle(str(said.get("means") or "")),
            "kind": kind,
        }
    return held


def save(held: dict, path: Path | None = None) -> None:
    where = path or VOCAB_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(held, indent=2, sort_keys=True), encoding="utf-8")


def forget(word: str, *, held: dict | None = None) -> dict:
    """`held` without `word`. Silent when it was never known."""
    said = " ".join((word or "").split()).casefold()
    return {key: value for key, value in (held or {}).items() if key != said}


def describe(held: dict) -> str:
    """The learned words, grouped by what they mean, for reading in Discord."""
    if not held:
        return (
            "I haven't been taught any lead-type words yet.\n"
            "Teach me one with `@RYTE words STNDRD = standard`."
        )
    by_kind: dict[str, list[str]] = {}
    for said in sorted(held.values(), key=lambda one: str(one["word"]).casefold()):
        by_kind.setdefault(str(said["kind"]), []).append(
            f"`{said['word']}` → {said['means']}"
        )
    lines = []
    for kind in ("family", "tier", "qualifier"):
        if by_kind.get(kind):
            lines.append(f"**{kind}**\n" + "\n".join(by_kind[kind]))
    return "\n\n".join(lines)
