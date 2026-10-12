"""The training pages on agentleadlab.com/training/... - Agent Success Full
Interviews, and one page per Success Stories section.

"ryte just have to duplicate the current section and swap out that yt link".
The pages are built in GHL's drag-and-drop editor - one row per video: the
video, its title, "with <name>", and a paragraph about it - and nothing outside
GHL can edit them. So RYTE keeps the list of videos it has added (and taken
off) on a WordPress data page, and `website/full-interviews-loader.html`, pasted
once into the Trainings website's tracking code, copies a page's own top row
for each one and swaps in its video, title, name and paragraph. Rows built by
hand in GHL stay as they are, except one RYTE was told to take off.

The Full Interviews page's list is the data's own "rows"/"gone" - where it
started - and every other page's is under "sections", by section name.
"""

from __future__ import annotations

from .segments import FULL_INTERVIEWS

DATA_SLUG = "ll-full-interviews-data"
DATA_TITLE = "Training pages data (RYTE)"
MARK = "LLFULL-"
WHAT = "the training pages"
PAGE_URL = "https://agentleadlab.com/training/agent-success-full-interviews"

#: Each Success Stories section's own page, as the loader knows them too.
PAGES = {
    FULL_INTERVIEWS: "agent-success-full-interviews",
    "Mortgage Protection Training": "mortgage-protection",
    "Final Expense Training": "final-expense",
    "IUL Training": "indexed-universal-life",
    "Veteran Training": "veteran-protection",
    "The Blueprint To Building Your Own Insurance System": "the-blueprint-to-building-your-own-insurance-system",
    "Why Agent Lead Lab": "why-choose-us",
    "Agent's Expectations": "agents-expectations",
    "Effective Strategies For Prospect Engagement": "effective-strategies-for-prospect-engagement",
    "Aged Leads": "aged-leads",
}


def empty() -> dict:
    return {"rows": [], "gone": [], "sections": {}}


def _list(one) -> dict:
    one = one if isinstance(one, dict) else {}
    return {"rows": [row for row in one.get("rows") or [] if isinstance(row, dict) and row.get("id")],
            "gone": [str(row) for row in one.get("gone") or [] if row]}


def _clean(data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    return {**data, **_list(data),
            "sections": {name: _list(one) for name, one in sections.items() if name in PAGES}}


def page(data: dict | None, section: str = FULL_INTERVIEWS) -> dict:
    """One page's {"rows", "gone"}."""
    data = _clean(data)
    return _list(data if section == FULL_INTERVIEWS else data["sections"].get(section))


def _put(data: dict, section: str, one: dict) -> dict:
    if section == FULL_INTERVIEWS:
        return {**data, **one}
    return {**data, "sections": {**data["sections"], section: one}}


def add(data: dict | None, row: dict, section: str = FULL_INTERVIEWS) -> dict:
    """The video first on its page. Already there: moved, not twice. Taken
    off before: back on."""
    if section not in PAGES:
        raise ValueError(f"There's no training page for “{section}”.")
    data = _clean(data)
    held = page(data, section)
    row = {key: " ".join(str(row.get(key) or "").split()) for key in ("id", "title", "name", "about")}
    rows = [one for one in held["rows"] if one.get("id") != row["id"]]
    gone = [one for one in held["gone"] if one != row["id"]]
    return _put(data, section, {"rows": [row] + rows, "gone": gone})


def remove(data: dict | None, video_id: str, section: str = FULL_INTERVIEWS) -> dict:
    """The video off its page - RYTE's row, or the one built by hand in GHL."""
    data = _clean(data)
    held = page(data, section)
    rows = [one for one in held["rows"] if one.get("id") != video_id]
    gone = held["gone"] + ([video_id] if video_id and video_id not in held["gone"] else [])
    return _put(data, section, {"rows": rows, "gone": gone})
