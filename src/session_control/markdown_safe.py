"""Render the small markdown subset memory notes use, safely.

Note text comes from session transcripts and model output, so it is untrusted. The
renderer escapes *everything* first and only then recognises a fixed set of patterns, so
no input can introduce an element or attribute of its own. Links and images are
deliberately not supported: a URL stays inert text.

Supported: ``#``-``######`` headings, ``-``/``*`` bullet lists, fenced code blocks,
paragraphs, ``**bold**`` and `` `inline code` ``.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_BULLET = re.compile(r"^[ \t]*[-*][ \t]+(.*)$")
_FENCE = re.compile(r"^[ \t]*(```|~~~~)")


def _inline(text: str) -> str:
    out = str(escape(text))
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    return _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)


def render_markdown(text: str) -> Markup:
    html: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    code: list[str] | None = None
    fence = ""

    def flush() -> None:
        if paragraph:
            html.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if bullets:
            html.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in (text or "").splitlines():
        marker = _FENCE.match(line)
        if code is not None:
            if marker and marker.group(1) == fence:
                html.append("<pre><code>" + "\n".join(code) + "</code></pre>")
                code = None
            else:
                code.append(str(escape(line)))
            continue
        if marker:
            flush()
            code, fence = [], marker.group(1)
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)  # keep the page's own <h1> unique
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet:
            if paragraph:
                flush()
            bullets.append(bullet.group(1))
        elif not line.strip():
            flush()
        else:
            if bullets:
                flush()
            paragraph.append(line.strip())
    if code is not None:  # unterminated fence: still show what was written
        html.append("<pre><code>" + "\n".join(code) + "</code></pre>")
    flush()
    return Markup("\n".join(html))  # noqa: S704 - built only from escaped text
