"""Every page loads the portfolio top bar, which the shared Caddy serves at /_portfolio/."""

from pathlib import Path

TAG = '<script src="/_portfolio/chrome.js" defer></script>'
ROOT = Path(__file__).resolve().parents[1]
FILES = ["src/session_control/templates/*.html"]


def test_every_page_loads_the_portfolio_top_bar():
    pages = [p for pattern in FILES for p in sorted(ROOT.glob(pattern))]
    assert pages
    for path in pages:
        text = path.read_text(encoding="utf-8")
        assert text.count("</head>") >= 1, path
        assert text.count(TAG) == text.count("</head>"), f"{path} has a page without the top bar"


THEME_TAGS = (
    '<link rel="stylesheet" href="/_portfolio/theme.css">',
    '<script src="/_portfolio/theme.js"></script>',
)


def test_pages_load_the_shared_theme_before_their_stylesheet():
    for path in sorted((ROOT / "src/session_control/templates").glob("*.html")):
        page = path.read_text(encoding="utf-8")
        for tag in THEME_TAGS:
            assert tag in page, path
        assert page.index(THEME_TAGS[0]) < page.index("styles.css"), path


def test_stylesheet_takes_colours_from_the_theme_tokens():
    import re

    css = (ROOT / "src/session_control/static/styles.css").read_text(encoding="utf-8")
    css = re.sub(r"var\([^)]*\)", "", css)  # fallbacks inside var() are fine
    lines = [ln for ln in css.splitlines() if "shadow" not in ln]
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", "\n".join(lines)) == []
