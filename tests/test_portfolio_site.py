"""The vendored portfolio seam: its fallback copy is intact and answers when nothing else does."""

import hashlib
from pathlib import Path

import pytest

from session_control.portfolio import site as portfolio_site

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "src/session_control/portfolio"


def test_manifest_pins_every_vendored_file():
    lines = (VENDORED / "MANIFEST.sha256").read_text().splitlines()
    names = set()
    for line in lines:
        digest, name = line.split("  ", 1)
        names.add(name)
        assert hashlib.sha256((VENDORED / name).read_bytes()).hexdigest() == digest, name
    assert names == {"__init__.py", "site.py", "theme.css", "theme.js", "chrome.js"}


def test_the_fallback_answers_with_no_store_and_serves_only_the_allowlist(tmp_path):
    site = portfolio_site.resolve(
        tmp_path, vendored=VENDORED, env={portfolio_site.STORE_ENV: str(tmp_path / "none")}
    )
    assert site.response("/_portfolio/theme.css")[0] == (VENDORED / "theme.css").read_bytes()
    assert site.response("/_portfolio/chrome.js")[0] == (VENDORED / "chrome.js").read_bytes()
    assert site.response("/_portfolio/nav.json")[0] == portfolio_site.EMPTY_NAV
    for path in ("/_portfolio/site.py", "/_portfolio/MANIFEST.sha256", "/_portfolio/../site.py"):
        assert site.response(path) is None


def test_the_example_config_is_synthetic_and_valid():
    tomllib = pytest.importorskip("tomllib")
    text = (ROOT / "config" / "portfolio-site.example.toml").read_text()
    settings = tomllib.loads(text)["site"]
    assert settings["fronting"] in portfolio_site.FRONTING
    assert set(settings) == {"fronting"}


def _check_route(client, monkeypatch, tmp_path):
    from session_control.portfolio import site as portfolio_site

    monkeypatch.setenv(portfolio_site.STORE_ENV, str(tmp_path / "no-store"))
    css = client.get("/_portfolio/theme.css")
    assert css.status_code == 200
    assert css.headers["Content-Type"].startswith("text/css")
    assert css.get_data() == (VENDORED / "theme.css").read_bytes()
    assert client.get("/_portfolio/chrome.js").headers["Content-Type"].startswith("text/javascript")
    assert client.get("/_portfolio/nav.json").get_data() == portfolio_site.EMPTY_NAV
    for path in ("/_portfolio/site.py", "/_portfolio/MANIFEST.sha256", "/_portfolio/other"):
        assert client.get(path).status_code == 404


def test_the_app_serves_the_portfolio_files_itself(app_config, monkeypatch, tmp_path):
    from session_control.web import create_app

    _check_route(create_app(app_config).test_client(), monkeypatch, tmp_path)
