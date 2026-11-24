"""What the agent marked on their own lead sheet, by colour.

Franklin's rebuttals lead with it: filter column A by fill, green = Sold, and
screenshot the green rows - "a lead that closed a sale is not a poor-quality
lead". The legend sits in the top rows of every sheet ("Green = Sold, Purple =
disconnected/wrong number, Blue = call blocker, Yellow = booked appointment,
Red = not interested"), so the colours are read off the legend itself rather
than assumed: a sheet whose green is a shade off still counts.

Pure: the cells come in as (text, (r, g, b) or None), 0-1 floats as Google
gives them. Reading them is `gsheets.SheetsClient.fills`.
"""

from __future__ import annotations

import re

#: Outcome, what the legend says for it, how it's said in a rebuttal.
OUTCOMES = (
    ("sold", re.compile(r"\bsold\b|\bsale\b|\bclosed\b", re.IGNORECASE), "sold"),
    ("appointment", re.compile(r"appointment|\bappt\b|booked", re.IGNORECASE), "appointment booked"),
    ("not_interested", re.compile(r"not\s+interested", re.IGNORECASE), "not interested"),
    ("disconnected", re.compile(r"disconnect|wrong\s+number", re.IGNORECASE), "disconnected / wrong number"),
    ("blocker", re.compile(r"call\s*block|blocker", re.IGNORECASE), "call blocker"),
)

#: Rows the legend can be in.
LEGEND_ROWS = 10
#: How far a fill can be from the legend's and still be that colour.
NEAR = 0.12


def _blank(rgb) -> bool:
    """No fill: none at all, or white/near-white."""
    return rgb is None or all(part >= 0.95 for part in rgb)


def _distance(one, two) -> float:
    return max(abs(a - b) for a, b in zip(one, two))


def legend(rows) -> tuple[dict, int]:
    """({outcome: rgb}, the last legend row's index) off the top rows.

    The colour is the cell's own fill, or - for "■ | Sold" laid out as a
    swatch beside a label - the nearest filled cell in the same row."""
    found, last = {}, -1
    for at, row in enumerate(rows[:LEGEND_ROWS]):
        for col, (text, rgb) in enumerate(row):
            for key, pattern, _said in OUTCOMES:
                if key in found or not pattern.search(str(text or "")):
                    continue
                colour = rgb if not _blank(rgb) else None
                if colour is None:
                    near = sorted((abs(col - other), cell[1]) for other, cell in enumerate(row)
                                  if other != col and not _blank(cell[1]))
                    colour = near[0][1] if near else None
                if colour is not None:
                    found[key] = tuple(colour)
                    last = max(last, at)
                break
    return found, last


def _by_hue(rgb) -> str:
    """Without a legend: green is sold, yellow an appointment - the two the
    rebuttal needs, and the two least likely to be anything else."""
    import colorsys

    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    hue *= 360
    if saturation < 0.08 or value < 0.35:
        return ""
    # Google's greens, light 3 to dark, sit between about 90 and 170 degrees.
    if 85 <= hue <= 170:
        return "sold"
    if 40 <= hue < 70 and saturation >= 0.15:
        return "appointment"
    return ""


def outcome_of(rgb, colours: dict) -> str:
    if _blank(rgb):
        return ""
    if colours:
        best = min(((_distance(rgb, colour), key) for key, colour in colours.items()), default=(1.0, ""))
        return best[1] if best[0] <= NEAR else ""
    return _by_hue(rgb)


def count(rows, *, column: int = 0) -> dict:
    """{"counts": {outcome: n}, "rows": {outcome: [sheet row numbers]},
    "leads": n, "legend": bool} - each lead row by its fill in `column`."""
    colours, last = legend(rows)
    counts, where, leads = {}, {}, 0
    for at, row in enumerate(rows):
        if at <= last or not row or column >= len(row):
            continue
        if not any(str(text or "").strip() for text, _rgb in row[:9]):
            continue
        text, rgb = row[column]
        # A header row: words, no fill, before any lead.
        if not leads and _blank(rgb) and re.search(r"[A-Za-z]{3}", str(text or "")) and not re.search(r"\d{3}", " ".join(str(t or "") for t, _ in row)):
            continue
        leads += 1
        outcome = outcome_of(rgb, colours)
        if outcome:
            counts[outcome] = counts.get(outcome, 0) + 1
            where.setdefault(outcome, []).append(at + 1)
    return {"counts": counts, "rows": where, "leads": leads, "legend": bool(colours)}


def said(got: dict) -> str:
    """"The agent marked 4 leads Sold (rows 12, 18, 25, 31) and 6 appointment
    booked on their own sheet." - or "" when nothing's marked."""
    counts = got.get("counts") or {}
    if not counts:
        return ""
    parts = []
    for key, _pattern, words in OUTCOMES:
        many = counts.get(key)
        if not many:
            continue
        rows = got["rows"].get(key) or []
        where = f" (rows {', '.join(map(str, rows[:12]))}{', …' if len(rows) > 12 else ''})" if key == "sold" else ""
        parts.append(f"{many} {('Sold' if key == 'sold' else words)}{where}")
    how = "by the colour legend on the sheet" if got.get("legend") else "by fill colour (green = sold, yellow = appointment)"
    return (f"Outcomes the client marked on their own sheet, {how}: " + "; ".join(parts)
            + f" - out of {got.get('leads', 0)} leads.")
