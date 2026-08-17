"""Which copy of the code is actually running.

A bot answering out of a stale checkout is indistinguishable from a fix that
didn't work - the same wrong behaviour, the same clean logs. So RYTE says its
commit out loud, and the question stops being a guess.
"""

from __future__ import annotations

import subprocess

from .config import REPO_ROOT


def code_version() -> str:
    """`8e976d4 Aug 17 14:32` for a checkout, or a plain note when it isn't one."""
    fields = _git("log", "-1", "--format=%h %ad", "--date=format:%b %d %H:%M")
    if not fields:
        return "unknown (not a git checkout)"
    return f"{fields}{' +local edits' if _git('status', '--porcelain') else ''}"


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
