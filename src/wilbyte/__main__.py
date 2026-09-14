"""`python -m wilbyte`, which is the way in that cannot come unstuck.

The `wilbyte` command in `.venv/bin` is a file pip generates, and finding the
package from it depends on the editable install pip also generated. Both of
those are links rather than the thing itself, and on the morning of the 14th
they pointed at nothing: the code was there, freshly pulled, and RYTE would
not start.

Run as a module with `src` on the path, none of that is in the way. The only
thing left that can break is the venv's own Python, which is the one piece
that has to work for anything else to.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
