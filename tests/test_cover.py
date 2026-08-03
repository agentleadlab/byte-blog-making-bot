import pytest

from wilbyte.cover import CoverError, build_html, render_cover
from wilbyte.models import CoverPlan


@pytest.fixture
def plan():
    return CoverPlan(
        kicker="AGED, FRESH, PREMIUM",
        headline="WHY MOST AGENTS NEVER MOVE UP A LEAD TIER",
    )


def test_html_has_no_unsubstituted_tokens(plan, config):
    import re

    markup = build_html(plan, config)

    assert re.findall(r"__[A-Z_]+__", markup) == []
    assert "AGED, FRESH, PREMIUM" in markup
    assert f"width: {config.cover.width}px" in markup


def test_html_escapes_text_so_quotes_do_not_break_the_markup(config):
    plan = CoverPlan(kicker="A & B", headline='WHY YOUR "DEAD" LEADS <ARE> FINE')
    markup = build_html(plan, config)

    assert "&amp;" in markup
    assert "&lt;ARE&gt;" in markup
    assert "<ARE>" not in markup


def test_the_brand_font_is_embedded_by_default(plan, config):
    """assets/fonts ships Anton, so covers render condensed out of the box."""
    markup = build_html(plan, config)

    assert "font-family: 'Anton'" in markup
    assert "font/woff2;base64," in markup


def test_renders_a_png_at_the_configured_size(plan, config, tmp_path):
    out = render_cover(plan, config, tmp_path / "cover.png")

    assert out.exists()
    assert out.stat().st_size > 5_000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # The intermediate HTML is cleaned up unless asked for.
    assert not (tmp_path / "cover.html").exists()


def test_keep_html_leaves_the_intermediate_file(plan, config, tmp_path):
    render_cover(plan, config, tmp_path / "cover.png", keep_html=True)

    assert (tmp_path / "cover.html").exists()


def test_missing_logo_falls_back_to_the_wordmark(plan, config, tmp_path):
    markup = build_html(plan, config, logo_path=tmp_path / "nope.png")

    assert '<div class="logo-fallback">Agent Lead Lab</div>' in markup
    assert "data:image/png;base64," not in markup


def test_logo_file_is_inlined_as_a_data_uri(plan, config, tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    markup = build_html(plan, config, logo_path=logo)

    assert "data:image/png;base64," in markup
    assert '<div class="logo-fallback">' not in markup


def test_brand_fonts_are_embedded_when_present(plan, config, tmp_path, monkeypatch):
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    (font_dir / "Anton-Regular.woff2").write_bytes(b"wOF2fake")

    import wilbyte.cover as cover_mod

    monkeypatch.setattr(cover_mod, "FONT_DIR", font_dir)
    markup = build_html(plan, config)

    assert "@font-face" in markup
    assert "font-family: 'Anton Regular'" in markup
    assert "font/woff2;base64," in markup
