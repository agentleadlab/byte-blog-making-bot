"""Channel names in the team's style: "✉️│ᴀᴅᴍɪɴ-ᴀɢᴇɴᴛʟᴇᴀᴅʟᴀʙ-ᴄᴏᴍ" - the emoji,
the bar, and the name in small capitals, the way the channel-name generator
writes them. And back again, so a styled name still reads as its letters."""

from __future__ import annotations

import re
import unicodedata

#: a-z in small capitals. No small x exists; the plain one is what the
#: generators use.
SMALL_CAPS = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_BACK = {value: key for key, value in SMALL_CAPS.items() if value != "x"}
# Other generators' small q and s.
_BACK.update({"ꞯ": "q", "ѕ": "s"})

#: The bar between the emoji and the name.
BAR = "│"
_BARS = "│|｜┃∣︱ǀ"


def plain(name: str) -> str:
    """A styled name in plain letters: "✉️│ᴀᴅᴍɪɴ-ᴄᴏᴍ" -> "✉️│admin-com"."""
    out = []
    for ch in str(name or ""):
        if ch in _BACK:
            out.append(_BACK[ch])
            continue
        if not ch.isascii():
            named = re.match(r"LATIN (?:SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL )?([A-Z])$",
                             unicodedata.name(ch, ""))
            if named:
                out.append(named.group(1).lower())
                continue
        out.append(ch)
    return "".join(out)


def _split(name: str) -> tuple[str, str]:
    """(the emoji and bar in front, the words)."""
    found = re.match(r"^([^A-Za-z0-9" + "".join(SMALL_CAPS.values()) + r"]*)(.*)$", str(name or ""), re.S)
    return (found.group(1), found.group(2)) if found else ("", str(name or ""))


def styled(name: str) -> str:
    """The name in the style: its emoji kept, the bar after it, the words in
    small capitals. Digits, dashes and underscores as they are."""
    front, words = _split(plain(name))
    front = front.strip()
    if front and not any(bar in front for bar in _BARS):
        front += BAR
    return front + "".join(SMALL_CAPS.get(ch, ch) for ch in words.lower())


def is_styled(name: str) -> bool:
    """Already in the style: no plain letters left in its words."""
    return styled(name) == str(name or "")
