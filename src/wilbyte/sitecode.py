"""Page scripts RYTE keeps on leadlabcrm.com, for GHL pages to load.

"now check the site/ghl codes": the training pages' script is eleven thousand
characters, and pasted into GHL it came out cut three times running - the top
gone, the rest shown on the page as text. So the pages get a few lines that
load it, and the script itself is kept here, beside the video lists, as a
WordPress data page RYTE writes whenever its copy in `website/` changes. A fix
to it reaches every page without anybody pasting anything again.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import REPO_ROOT

MARK = "LLJS-"

#: name -> the snippet file the script is in. The data page is "ll-code-<name>".
SCRIPTS = {"training-pages": REPO_ROOT / "website" / "full-interviews-loader.html"}


def slug(name: str) -> str:
    return f"ll-code-{name}"


def script_of(html: str) -> str:
    """The code inside a snippet's <script> tag."""
    found = re.search(r"<script>\s*(.*?)\s*</script>", str(html or ""), re.DOTALL)
    if not found:
        raise ValueError("No <script> in that snippet.")
    return found.group(1)


def code(name: str, *, path: Path | None = None) -> str:
    return script_of((path or SCRIPTS[name]).read_text(encoding="utf-8"))
