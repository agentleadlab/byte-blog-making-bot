"""Render the blog cover image.

Replaces the Canva step: the Agent Lead Lab thumbnail is rebuilt as an HTML/CSS
template and screenshotted with Chromium at the 600x400 size GHL recommends.

Drop a logo at `assets/logo.png` and it gets used; without one the template
falls back to a wordmark.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path

from .config import Config, REPO_ROOT
from .models import CoverPlan

TEMPLATE_PATH = Path(__file__).parent / "templates" / "cover.html"
LOGO_PATH = REPO_ROOT / "assets" / "logo.png"
FONT_DIR = REPO_ROOT / "assets" / "fonts"

_FONT_MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf"}


class CoverError(RuntimeError):
    """Raised when the cover image cannot be rendered."""


def build_html(plan: CoverPlan, config: Config, *, logo_path: Path | None = None) -> str:
    """Substitute the plan into the template. Returns standalone HTML."""
    if not TEMPLATE_PATH.exists():
        raise CoverError(f"Cover template missing: {TEMPLATE_PATH}")

    width, height = config.cover.width, config.cover.height
    scale = height / 400  # the template's sizes are tuned at 600x400

    logo_file = logo_path if logo_path is not None else LOGO_PATH
    if logo_file and logo_file.exists():
        data = base64.b64encode(logo_file.read_bytes()).decode("ascii")
        suffix = logo_file.suffix.lstrip(".").lower() or "png"
        mime = "image/svg+xml" if suffix == "svg" else f"image/{'jpeg' if suffix in ('jpg', 'jpeg') else suffix}"
        logo_html = f'<img src="data:{mime};base64,{data}" alt="">'
    else:
        logo_html = '<div class="logo-fallback">Agent Lead Lab</div>'

    replacements = {
        "__FONT_FACES__": _font_faces(),
        "__WIDTH__": width,
        "__HEIGHT__": height,
        "__PAD__": round(28 * scale),
        "__LOGO_H__": round(34 * scale),
        "__LOGO_GAP__": round(14 * scale),
        "__KICKER_GAP__": round(12 * scale),
        "__KICKER_SIZE__": round(26 * scale),
        "__KICKER_MAX_H__": round(44 * scale),
        "__HEADLINE_SIZE__": round(58 * scale),
        "__SHADOW__": round(2 * scale),
        "__SHADOW_BLUR__": round(10 * scale),
        "__LOGO_HTML__": logo_html,
        "__KICKER__": html.escape(plan.kicker),
        "__HEADLINE__": html.escape(plan.headline),
    }

    markup = TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in replacements.items():
        markup = markup.replace(token, str(value))

    leftover = re.findall(r"__[A-Z_]+__", markup)
    if leftover:
        raise CoverError(f"Unsubstituted template tokens: {sorted(set(leftover))}")
    return markup


def _font_faces(font_dir: Path | None = None) -> str:
    """Embed any brand fonts dropped into `assets/fonts/` as @font-face rules.

    The template's font stack asks for a condensed display face (Anton/Oswald).
    Without one it falls back to a system sans, which renders wider than the
    Canva original - dropping the .woff2 in here restores the exact look.
    """
    directory = font_dir if font_dir is not None else FONT_DIR
    if not directory.exists():
        return ""

    rules = []
    for font_file in sorted(directory.iterdir()):
        mime = _FONT_MIME.get(font_file.suffix.lower())
        if not mime:
            continue
        data = base64.b64encode(font_file.read_bytes()).decode("ascii")
        family = font_file.stem.replace("_", " ").replace("-", " ")
        rules.append(
            f"@font-face {{ font-family: '{family}'; "
            f"src: url(data:{mime};base64,{data}); font-display: block; }}"
        )
    return "\n  ".join(rules)


def render_cover(
    plan: CoverPlan,
    config: Config,
    output_path: Path,
    *,
    logo_path: Path | None = None,
    keep_html: bool = False,
) -> Path:
    """Render `plan` to a PNG at `output_path`. Returns the path written."""
    markup = build_html(plan, config, logo_path=logo_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    html_path = output_path.with_suffix(".html")
    html_path.write_text(markup, encoding="utf-8")

    try:
        _screenshot(html_path, output_path, config.cover.width, config.cover.height)
    finally:
        if not keep_html:
            html_path.unlink(missing_ok=True)

    return output_path


def _chromium_executable() -> str | None:
    """Locate a pre-installed Chromium.

    Managed environments often ship a Chromium build whose revision does not match
    the pinned Playwright version, so prefer an explicit binary over Playwright's
    own lookup and only fall back to the default when nothing is found.
    """
    import os

    override = os.getenv("WILBYTE_CHROMIUM_PATH")
    if override and Path(override).exists():
        return override

    roots = [Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))]
    for root in roots:
        if not root.exists():
            continue
        direct = root / "chromium"
        if direct.exists() and not direct.is_dir():
            return str(direct)
        candidates = sorted(root.glob("chromium-*/chrome-linux/chrome"))
        candidates += sorted(root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
        if candidates:
            return str(candidates[-1])
    return None


def _screenshot(html_path: Path, png_path: Path, width: int, height: int) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise CoverError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    launch_kwargs: dict = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    executable = _chromium_executable()
    if executable:
        launch_kwargs["executable_path"] = executable

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(html_path.resolve().as_uri())
                page.wait_for_function("document.documentElement.dataset.ready === '1'", timeout=10_000)
                page.screenshot(path=str(png_path), clip={"x": 0, "y": 0, "width": width, "height": height})
            finally:
                browser.close()
    except CoverError:
        raise
    except Exception as exc:
        raise CoverError(f"Chromium failed to render the cover image: {exc}") from exc
