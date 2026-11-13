"""The note renderer must turn untrusted text into inert HTML."""

from __future__ import annotations

import re

import pytest

from session_control.markdown_safe import render_markdown

_ALLOWED_TAG = re.compile(r"</?(?:p|h[1-6]|ul|li|strong|code|pre)>")


def test_renders_the_supported_subset():
    html = str(
        render_markdown(
            "# Title\n\n### Goal\n- ship **it** now\n- keep `code`\n\nsome text\n"
            "continues\n\n```\nraw <b>\n```\n"
        )
    )
    assert "<h2>Title</h2>" in html and "<h4>Goal</h4>" in html
    assert "<li>ship <strong>it</strong> now</li>" in html
    assert "<li>keep <code>code</code></li>" in html
    assert "<p>some text continues</p>" in html
    assert "<pre><code>raw &lt;b&gt;</code></pre>" in html


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[click](javascript:alert(1))",
        "**<script>x</script>**",
        "`<script>x</script>`",
        "- <iframe src=//evil></iframe>",
        "### <svg onload=alert(1)>",
        '"><script>x</script>',
        "```\n</code></pre><script>x</script>\n```",
    ],
)
def test_untrusted_text_never_becomes_markup(payload):
    html = str(render_markdown(payload))
    # Only the renderer's own, attribute-free tags may remain.
    assert "<" not in _ALLOWED_TAG.sub("", html)
    assert "<script" not in html and "<a " not in html


def test_links_stay_inert_and_unterminated_fences_render():
    assert "<a" not in str(render_markdown("[x](https://example.com)"))
    assert "<pre><code>still open" in str(render_markdown("```\nstill open"))
    assert str(render_markdown("")) == ""
