"""What the team taught Ryte, kept and read before it asks anyone anything.

"i feel like he's not learning and is going dumb everyday": Rook's "it's an
AGED order, no hub row needed, OK to close" closed Christopher Gallion's card
and taught nothing - the next $100 Zelle was asked about all the same.

Two kinds:

- **Told**: `@RYTE lesson: aged orders never have a hub row` - Franklin's own
  words, kept until forgotten.
- **Learned**: every flag somebody explained away - OK as is, Rook's "right as
  it is", the team's answer in #rook-qc, a list cleared as good to go - read
  back off the payments Ryte follows. Nothing to keep twice: the payment
  record is the memory.

And **misses**: `@RYTE you missed Bryce Evans - paid, nobody set it up`,
counted on the scorecard and kept as a lesson.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .state import _state_dir

LESSONS_PATH = _state_dir() / "lessons.json"

#: Told lessons read into every question, newest first.
MOST_TOLD = 20
#: Learned ones read in, the ones about the same kind of problem first.
MOST_LEARNED = 10


def _load(path: Path | None = None) -> dict:
    try:
        held = json.loads((path or LESSONS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = {}
    if not isinstance(held, dict):
        held = {}
    held.setdefault("told", [])
    held.setdefault("misses", [])
    return held


def _save(held: dict, path: Path | None = None) -> None:
    where = path or LESSONS_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def teach(text: str, by: str = "", path: Path | None = None) -> int:
    """A lesson in Franklin's words. Its number."""
    text = " ".join(str(text or "").split())
    held = _load(path)
    held["told"].append({"text": text, "by": by, "at": _now()})
    _save(held, path)
    return len(held["told"])


def missed(text: str, by: str = "", path: Path | None = None) -> int:
    """Something the team caught that Ryte didn't - a miss, and a lesson."""
    text = " ".join(str(text or "").split())
    held = _load(path)
    held["misses"].append({"text": text, "by": by, "at": _now()})
    _save(held, path)
    return len(held["misses"])


def told(path: Path | None = None) -> list[dict]:
    return list(_load(path)["told"])


def misses(path: Path | None = None) -> list[dict]:
    return list(_load(path)["misses"])


def forget(number: int, path: Path | None = None) -> str:
    """A told lesson taken back, by its number. Its words, or ""."""
    held = _load(path)
    if not 1 <= number <= len(held["told"]):
        return ""
    gone = held["told"].pop(number - 1)
    _save(held, path)
    return gone.get("text", "")


# ------------------------------------------------------------ the learned

#: What kind of problem a flag was, for the scorecard and for picking the
#: lessons that matter to a question. First that fits.
KINDS = (
    ("not set up", re.compile(r"not on the hub|nothing on the hub|not sure which order|no setup|not set up",
                              re.IGNORECASE)),
    ("lead type", re.compile(r"lead type|\btier\b|\bstandard\b|\bplus\b|\bproduct\b", re.IGNORECASE)),
    ("states", re.compile(r"\bstates?\b", re.IGNORECASE)),
    ("email", re.compile(r"\bemail\b", re.IGNORECASE)),
    ("sheet", re.compile(r"\bsheet\b", re.IGNORECASE)),
    ("contract", re.compile(r"contract|signed|pandadoc", re.IGNORECASE)),
    ("schedule", re.compile(r"schedule|\bcap\b|hours|per day|/day", re.IGNORECASE)),
    ("count", re.compile(r"#|\bordered\b|\bleads?\b|\bcount\b|delivered|short", re.IGNORECASE)),
)


def kind(problem: str) -> str:
    said = str(problem or "")
    return next((name for name, pattern in KINDS if pattern.search(said)), "other")


def learned(records: dict) -> list[dict]:
    """Every flag somebody explained away, off the payments Ryte follows:
    [{"problem", "why", "by", "client", "at", "kind"}], newest first."""
    out = []
    for one in (records or {}).values():
        client = str(one.get("client") or one.get("name") or "")
        at = str(one.get("checked") or one.get("posted") or "")
        why = str(one.get("ok_by") or "")
        for problem in one.get("ok_problems") or []:
            out.append({"problem": str(problem), "why": why or "OK'd as it is", "by": why, "client": client,
                        "at": at, "kind": kind(problem)})
        if one.get("read_verdict") == "right_as_is" and one.get("read_reason"):
            for problem in one.get("asked_problems") or []:
                out.append({"problem": str(problem), "why": str(one["read_reason"]), "by": "Rook/Nova",
                            "client": client, "at": at, "kind": kind(problem)})
        if one.get("team_note"):
            for problem in one.get("open_problems") or one.get("asked_problems") or []:
                out.append({"problem": str(problem), "why": f"the team: {one['team_note']}", "by": "the team",
                            "client": client, "at": at, "kind": kind(problem)})
    seen, kept = set(), []
    for one in sorted(out, key=lambda item: item["at"], reverse=True):
        key = (one["client"].casefold(), one["kind"], one["why"][:60])
        if key in seen:
            continue
        seen.add(key)
        kept.append(one)
    return kept


def for_question(records: dict, problems: list, path: Path | None = None) -> list[str]:
    """The lessons to read before asking about these problems: every told
    one, every miss, then what was learned about the same kinds of problem."""
    told_ = [f"Franklin: {one['text']}" for one in reversed(told(path))][:MOST_TOLD]
    missed_ = [f"Missed before (don't again): {one['text']}" for one in reversed(misses(path))][:MOST_TOLD]
    kinds = {kind(one) for one in problems or []}
    learned_ = [f"{one['client'] or 'A client'}: flagged “{one['problem'][:120]}” - it was fine: "
                f"{one['why'][:200]}"
                for one in learned(records) if one["kind"] in kinds][:MOST_LEARNED]
    return told_ + missed_ + learned_


def listed(records: dict, path: Path | None = None) -> str:
    """`@RYTE lessons` - what Ryte has been taught, and how much it's learned."""
    told_ = told(path)
    lines = ["🧠 **What I've been taught**"]
    if told_:
        lines += [f"**{at}.** {one['text']}" + (f" -# {one['by']}" if one.get("by") else "")
                  for at, one in enumerate(told_, start=1)]
    else:
        lines.append("Nothing told yet - `@RYTE lesson: aged orders never have a hub row`.")
    miss = misses(path)
    if miss:
        lines.append(f"\n**Misses you told me about** ({len(miss)})")
        lines += [f"• {one['text']}" for one in miss[-8:]]
    got = learned(records)
    if got:
        by_kind = {}
        for one in got:
            by_kind[one["kind"]] = by_kind.get(one["kind"], 0) + 1
        lines.append(f"\n**Learned from flags explained away:** {len(got)} - "
                     + ", ".join(f"{name} {many}" for name, many in sorted(by_kind.items(), key=lambda kv: -kv[1])))
        lines += [f"• {one['client'] or '?'}: {one['problem'][:80]} - {one['why'][:100]}" for one in got[:5]]
    lines.append("-# `@RYTE forget lesson 2` takes one back.")
    return "\n".join(lines)
