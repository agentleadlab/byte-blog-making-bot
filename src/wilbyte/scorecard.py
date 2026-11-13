"""How Ryte is doing at QC, counted - not felt.

Every problem Ryte flagged (put on a card with people tagged, or asked Rook and
Nova about) ends one of three ways:

- **real**: it went away because somebody fixed it - the hub changed;
- **false alarm**: somebody explained it away - OK as is, Rook's "right as it
  is", the team's answer, cleared off the list as good to go;
- **still open**.

Plus **misses**: what the team caught that Ryte didn't, told with
`@RYTE you missed ...`. "Right" is real ÷ (real + false alarms).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import lessons


def _when(said) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(said or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def flags_of(one: dict) -> dict:
    """{problem key: {"text", "at"}} - what this payment was flagged for."""
    texts = one.get("flag_texts") or {}
    if texts:
        return texts
    # Payments flagged before the words were kept: the keys, dated by the payment.
    return {key: {"text": key, "at": one.get("posted") or one.get("checked") or ""}
            for key in one.get("ever_flagged") or []}


def outcomes(records: dict, *, since: datetime, until: datetime | None = None) -> list[dict]:
    """[{"client", "problem", "kind", "outcome"}] for every flag raised in the window."""
    from .rookqc import among, problem_key

    out = []
    for one in (records or {}).values():
        explained = [*(one.get("ok_problems") or []), *(one.get("cleared_problems") or [])]
        still = {problem_key(problem) for problem in one.get("open_problems") or []}
        for key, flag in flags_of(one).items():
            at = _when(flag.get("at"))
            if at is None or at < since or (until is not None and at >= until):
                continue
            text = str(flag.get("text") or key)
            if among(text, explained) or (one.get("team_note") and problem_key(text) not in still):
                outcome = "false alarm"
            elif problem_key(text) in still:
                outcome = "open"
            else:
                outcome = "real"
            out.append({"client": str(one.get("client") or one.get("name") or ""), "problem": text,
                        "kind": lessons.kind(text), "outcome": outcome})
    return out


def score(records: dict, *, now: datetime, days: int = 7, missed: list | None = None) -> dict:
    since = now - timedelta(days=days)
    found = outcomes(records, since=since)
    count = {name: sum(1 for one in found if one["outcome"] == name) for name in ("real", "false alarm", "open")}
    judged = count["real"] + count["false alarm"]
    misses = [one for one in (missed if missed is not None else lessons.misses())
              if (_when(one.get("at")) or now) >= since]
    by_kind = {}
    for one in found:
        by_kind.setdefault(one["kind"], {"real": 0, "false alarm": 0, "open": 0})[one["outcome"]] += 1
    before = outcomes(records, since=since - timedelta(days=days), until=since)
    real_before = sum(1 for one in before if one["outcome"] == "real")
    judged_before = real_before + sum(1 for one in before if one["outcome"] == "false alarm")
    return {"days": days, "flags": len(found), **count, "misses": len(misses), "miss_list": misses,
            "right": round(100 * count["real"] / judged) if judged else None,
            "right_before": round(100 * real_before / judged_before) if judged_before else None,
            "by_kind": by_kind, "false_alarms": [one for one in found if one["outcome"] == "false alarm"]}


def said(got: dict) -> str:
    """`@RYTE scorecard`."""
    lines = [f"📊 **Ryte's scorecard - last {got['days']} days**",
             f"Flags raised: **{got['flags']}** · ✅ real (fixed after) **{got['real']}** · "
             f"👌 false alarm **{got['false alarm']}** · ⏳ still open **{got['open']}** · "
             f"❌ misses you told me about **{got['misses']}**"]
    if got["right"] is not None:
        trend = ""
        if got["right_before"] is not None:
            arrow = "⬆️" if got["right"] > got["right_before"] else "⬇️" if got["right"] < got["right_before"] else "➡️"
            trend = f" {arrow} from {got['right_before']}% the {got['days']} days before"
        lines.append(f"**Right {got['right']}% of the time**{trend}")
    else:
        lines.append("Nothing settled yet to score.")
    kinds = sorted(got["by_kind"].items(), key=lambda kv: -kv[1]["false alarm"])
    worst = [f"{name} {counts['false alarm']}" for name, counts in kinds if counts["false alarm"]]
    if worst:
        lines.append("**False alarms by kind:** " + ", ".join(worst) + " - the first one is what to fix next")
    best = [f"{name} {counts['real']}" for name, counts in sorted(got["by_kind"].items(), key=lambda kv: -kv[1]["real"])
            if counts["real"]]
    if best:
        lines.append("**Real catches by kind:** " + ", ".join(best))
    if got["false_alarms"]:
        lines.append("\n**Latest false alarms**")
        lines += [f"• {one['client'] or '?'}: {one['problem'][:110]}" for one in got["false_alarms"][-5:]]
    if got["miss_list"]:
        lines.append("\n**Misses**")
        lines += [f"• {one['text'][:140]}" for one in got["miss_list"][-5:]]
    lines.append("-# `@RYTE you missed ...` when the team catches something I didn't - it's counted, and kept "
                 "as a lesson. `@RYTE lessons` shows what I've been taught.")
    return "\n".join(lines)


def one_line(got: dict) -> str:
    """The scorecard on the evening list, in a line."""
    if not got["flags"] and not got["misses"]:
        return ""
    right = f" ({got['right']}% right)" if got["right"] is not None else ""
    return (f"-# 📊 Ryte, last {got['days']} days: {got['real']} real catches, {got['false alarm']} false alarms"
            f"{right}, {got['misses']} missed · `@RYTE scorecard`")
