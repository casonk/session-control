"""Mask secret-looking values before session text leaves its provider's storage.

Handoff briefs copy recent conversation turns into a new file that another assistant
reads and may echo or log. Transcripts routinely hold pasted OAuth callback URLs, bearer
tokens and ``KEY=value`` lines, so those are replaced with ``[REDACTED]`` first.

This is a best-effort safety net, not a guarantee: it targets well-known shapes and
named secret fields, and prefers masking too much over leaking. Text with no
secret-like value in it is returned unchanged.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

# URL query / form parameters that carry credentials or one-time OAuth values.
_SENSITIVE_PARAMS = (
    "code",
    "state",
    "code_verifier",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "client_secret",
    "api_key",
    "apikey",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
)
_QUERY_PARAM = re.compile(
    r"([?&](?:"
    + "|".join(re.escape(p) for p in _SENSITIVE_PARAMS)
    + r")=)(?!\[REDACTED\])[^&\s\"'<>`)\]]+",
    re.IGNORECASE,
)

# Credentials embedded in a URL authority: scheme://user:password@host
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.IGNORECASE)

# Authorization header values.
_AUTH_HEADER = re.compile(
    r"\b(Bearer|Basic)\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE
)

# Named secret fields: KEY=value, KEY: value, "key": "value".
_NAMED_SECRET = re.compile(r"""(?ix)
    (\b[\w.-]*(?:secret|token|passw(?:or)?d|passwd|api[_-]?key|private[_-]?key|credential)[\w.-]*
     ["']?\s*[:=]\s*["']?)
    (?!\[REDACTED\])
    [^\s"',;&)\]}]{8,}
    """)

# Well-known token shapes, matched anywhere in the text.
_TOKEN_SHAPES = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bya29\.[A-Za-z0-9_-]{20,}",  # Google OAuth access token
        r"(?<![\w/])1//[A-Za-z0-9_-]{20,}",  # Google OAuth refresh token
        r"(?<![\w/])4/0[A-Za-z0-9_-]{20,}",  # Google OAuth authorization code
        r"\bGOCSPX-[A-Za-z0-9_-]{16,}",  # Google OAuth client secret
        r"\bAIza[A-Za-z0-9_-]{35}\b",  # Google API key
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",  # GitHub tokens
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bglpat-[A-Za-z0-9_-]{16,}",  # GitLab
        r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}",  # OpenAI / Anthropic style keys
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",  # Slack
        r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
        r"\bnpm_[A-Za-z0-9]{30,}",
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
    )
)


def redact(text: str) -> tuple[str, int]:
    """Return ``text`` with secret-like values masked, and how many were masked."""
    if not text:
        return text, 0
    before = text.count(REDACTED)
    text = _PEM_BLOCK.sub(REDACTED, text)
    text = _QUERY_PARAM.sub(rf"\1{REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}\2", text)
    text = _AUTH_HEADER.sub(rf"\1 {REDACTED}", text)
    for shape in _TOKEN_SHAPES:
        text = shape.sub(REDACTED, text)
    text = _NAMED_SECRET.sub(rf"\1{REDACTED}", text)
    return text, text.count(REDACTED) - before
