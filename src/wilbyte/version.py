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


def update_waiting() -> str:
    """The one-line summary of a newer commit on the remote, or "".

    Only ever reports a *fast-forward*: local commits or a diverged history
    mean someone is working in this checkout, and pulling out from under them
    would be rude at best.
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        return ""
    # A network round trip, so it gets longer than the local commands do. A
    # failed fetch just means no news - the next check tries again.
    _git("fetch", "--quiet", "origin", branch, check=False, timeout=30)
    behind = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    ahead = _git("rev-list", "--count", f"origin/{branch}..HEAD")
    if not behind.isdigit() or int(behind) == 0:
        return ""
    if ahead.isdigit() and int(ahead) > 0:
        return ""
    return _git("log", "-1", "--format=%h %s", f"origin/{branch}")


def _git(*args: str, check: bool = True, timeout: float = 5) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return "" if check else "ok"
    return result.stdout.strip() or ("" if check else "ok")
