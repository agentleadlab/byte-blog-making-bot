"""The Agent Success Full Interviews page on agentleadlab.com.

"ryte just have to duplicate the current section and swap out that yt link".
The page is built in GHL's drag-and-drop editor - one row per interview: the
video, its title, "with <name>", and a paragraph about it - and nothing outside
GHL can edit it. So RYTE keeps the list of interviews it has added (and taken
off) on a WordPress data page, and `website/full-interviews-loader.html`, pasted
once into the page's tracking code, copies the page's own top row for each one
and swaps in its video, title, name and paragraph. Rows built by hand in GHL
stay as they are, except one RYTE was told to take off.
"""

from __future__ import annotations

DATA_SLUG = "ll-full-interviews-data"
DATA_TITLE = "Full interviews page data (RYTE)"
MARK = "LLFULL-"
WHAT = "the Agent Success Full Interviews page"
PAGE_URL = "https://agentleadlab.com/training/agent-success-full-interviews"


def empty() -> dict:
    return {"rows": [], "gone": []}


def _clean(data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    rows = [one for one in data.get("rows") or [] if isinstance(one, dict) and one.get("id")]
    gone = [str(one) for one in data.get("gone") or [] if one]
    return {**data, "rows": rows, "gone": gone}


def add(data: dict | None, row: dict) -> dict:
    """The interview first on the page. Already there: moved, not twice. Taken
    off before: back on."""
    data = _clean(data)
    row = {key: " ".join(str(row.get(key) or "").split()) for key in ("id", "title", "name", "about")}
    rows = [one for one in data["rows"] if one.get("id") != row["id"]]
    gone = [one for one in data["gone"] if one != row["id"]]
    return {**data, "rows": [row] + rows, "gone": gone}


def remove(data: dict | None, video_id: str) -> dict:
    """The interview off the page - RYTE's row, or the one built by hand in GHL."""
    data = _clean(data)
    rows = [one for one in data["rows"] if one.get("id") != video_id]
    gone = data["gone"] + ([video_id] if video_id and video_id not in data["gone"] else [])
    return {**data, "rows": rows, "gone": gone}
