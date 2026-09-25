"""What RYTE spends on Claude, and on what.

"why is ryte spending so much". The Anthropic console shows the bill for the
key, not which of RYTE's jobs ran it up - the responder, the blog posts, the
rebuttals and the lessons all go through the one key. So every call is
counted here as it comes back: the tokens Claude reports on the response,
priced, filed under the job that asked. `@RYTE cost` reads it back.

Counting never gets in the way: a call whose usage can't be read or saved
still returns its answer.
"""

from __future__ import annotations

import functools
import json
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

from .state import _state_dir

USAGE_PATH = _state_dir() / "usage.json"

#: Days kept.
KEEP_DAYS = 120

#: Dollars per million tokens: (input, output). A cache write is 1.25x the
#: input price and a cache read 0.1x.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: The job a call is filed under, by the function that made it.
JOBS = {
    "draft_like_faith": "Responder drafts",
    "explain_the_change": "Responder learning",
    "explain_her_replies": "Responder learning",
    "faith_playbook": "Faith's playbook",
    "faith_answers": "Asking how Faith answers",
    "read_texts_off": "Reading text screenshots",
    "suggestions": "Suggestions",
    "_read_tags_fresh": "Reading tags",
    "sort_exhibits": "Dispute rebuttals",
    "write_rebuttal": "Dispute rebuttals",
    "write_sop_summary": "SOP summaries",
    "summarise_page": "Page summaries",
    "summarise_text": "Text summaries",
    "check_anthropic": "Health check",
}

#: ...and by the module, where one module is one job.
MODULES = {
    "wilbyte.copywriter": "Blog posts",
    "wilbyte.writer": "Blog posts",
    "wilbyte.segments": "Blog posts",
    "wilbyte.pipeline": "Blog posts",
}

_LOCK = threading.Lock()


def price(model: str, *, fresh: int = 0, out: int = 0, wrote: int = 0, read: int = 0) -> float:
    """Dollars for one call's tokens."""
    model = str(model or "")
    per_in, per_out = next(
        (both for name, both in sorted(PRICES.items(), key=lambda one: -len(one[0]))
         if model.startswith(name)),
        PRICES["claude-opus-5"],
    )
    return (fresh * per_in + wrote * per_in * 1.25 + read * per_in * 0.1 + out * per_out) / 1_000_000


def job_of(frame) -> str:
    """The job a call belongs to: the nearest function of RYTE's that is a
    known job, else its module's job, else the outermost of RYTE's functions."""
    outermost = ""
    by_module = ""
    while frame is not None:
        module = str(frame.f_globals.get("__name__") or "")
        if module.startswith("wilbyte") and module != __name__:
            name = frame.f_code.co_name
            if name in JOBS:
                return JOBS[name]
            by_module = by_module or MODULES.get(module, "")
            outermost = name
        frame = frame.f_back
    return by_module or outermost or "Other"


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or USAGE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        data = {"days": {}}
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or USAGE_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(data), encoding="utf-8")
    spare.replace(where)


def _count(usage, name: str) -> int:
    try:
        return int(getattr(usage, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def record(job: str, model: str, usage, *, day: date | None = None, path: Path | None = None) -> None:
    """One call, added to today's count for its job."""
    if usage is None:
        return
    fresh, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    wrote, read = _count(usage, "cache_creation_input_tokens"), _count(usage, "cache_read_input_tokens")
    day = day or date.today()
    with _LOCK:
        data = load(path)
        today = data["days"].setdefault(day.isoformat(), {})
        one = today.setdefault(job, {"calls": 0, "in": 0, "out": 0, "wrote": 0, "read": 0, "usd": 0.0})
        one["calls"] += 1
        one["in"] += fresh
        one["out"] += out
        one["wrote"] += wrote
        one["read"] += read
        one["usd"] = round(one["usd"] + price(model, fresh=fresh, out=out, wrote=wrote, read=read), 6)
        oldest = (day - timedelta(days=KEEP_DAYS)).isoformat()
        for gone in [one for one in data["days"] if one < oldest]:
            data["days"].pop(gone)
        save(data, path)


def install() -> None:
    """Count every call made through the Anthropic SDK from here on."""
    from anthropic.resources.messages import Messages

    if getattr(Messages.create, "_counted", False):
        return
    real = Messages.create

    @functools.wraps(real)
    def create(self, *args, **kwargs):
        got = real(self, *args, **kwargs)
        try:
            record(job_of(sys._getframe(1)), kwargs.get("model") or getattr(got, "model", ""),
                   getattr(got, "usage", None))
        except Exception:
            pass
        return got

    create._counted = True
    Messages.create = create


def totals(data: dict, days: int, *, today: date | None = None) -> dict:
    """{job: summed counts} over the last `days` days, today included."""
    today = today or date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    summed: dict[str, dict] = {}
    for day, jobs in data.get("days", {}).items():
        if day < since or day > today.isoformat():
            continue
        for job, one in jobs.items():
            into = summed.setdefault(job, {"calls": 0, "in": 0, "out": 0, "wrote": 0, "read": 0, "usd": 0.0})
            for key in into:
                into[key] += one.get(key, 0)
    return summed


def report(data: dict, *, today: date | None = None) -> str:
    """For Discord: today, the last 7 days and the last 30, by job."""
    today = today or date.today()
    kept = sorted(data.get("days", {}))
    if not kept:
        return ("Nothing counted yet - I count every Claude call from now on. "
                "Ask again tomorrow.")
    lines = []
    for label, days in (("Today", 1), ("Last 7 days", 7), ("Last 30 days", 30)):
        jobs = totals(data, days, today=today)
        spent = sum(one["usd"] for one in jobs.values())
        lines.append(f"**{label}: ${spent:,.2f}**")
        if days == 1 and not jobs:
            lines.append("-# nothing yet today")
    week = totals(data, 7, today=today)
    if week:
        lines.append("\n**What the last 7 days went on:**")
        for job, one in sorted(week.items(), key=lambda both: -both[1]["usd"]):
            each = one["usd"] / one["calls"] if one["calls"] else 0
            fed = one["in"] + one["wrote"] + one["read"]
            cached = f", {one['read'] * 100 // fed}% from cache" if fed and one["read"] else ""
            lines.append(
                f"• {job}: ${one['usd']:,.2f} — {one['calls']} call"
                f"{'' if one['calls'] == 1 else 's'}, ~${each:,.3f} each "
                f"({fed:,} tokens read{cached}, {one['out']:,} written)"
            )
    lines.append(f"-# Counting since {kept[0]}.")
    return "\n".join(lines)
