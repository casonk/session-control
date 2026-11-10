"""Secret masking for text copied into handoff briefs."""

from __future__ import annotations

import pytest

from session_control import handoff
from session_control.redaction import REDACTED, redact
from tests.helpers import seed_codex, write_jsonl

# Fake secrets are assembled from fragments so scanners do not flag this file.
BODY = "AbCdEf0123456789xyzUVW_-"
GOOGLE_ACCESS = "ya" + "29." + BODY
GOOGLE_REFRESH = "1/" + "/" + BODY
GOOGLE_CODE = "4/" + "0A" + BODY
GOOGLE_CLIENT_SECRET = "GOC" + "SPX-" + BODY
GITHUB = "gh" + "p_" + "A" * 36
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
JWT = "ey" + "Jhbc012345" + ".ey" + "Jzdb012345" + ".sig0123456"


@pytest.mark.parametrize(
    "secret",
    [GOOGLE_ACCESS, GOOGLE_REFRESH, GOOGLE_CODE, GOOGLE_CLIENT_SECRET, GITHUB, AWS, JWT],
)
def test_known_token_shapes_are_masked(secret):
    text, count = redact(f"here is the value {secret} for you")
    assert secret not in text
    assert text == f"here is the value {REDACTED} for you"
    assert count == 1


def test_oauth_callback_url_loses_code_and_state_but_keeps_the_rest():
    url = (
        "http://127.0.0.1:39847/callback?state=Zm9vYmFy0123456789&iss=https://accounts.example"
        f"&code={GOOGLE_CODE}&scope=read"
    )
    text, count = redact(url)
    assert "Zm9vYmFy0123456789" not in text and GOOGLE_CODE not in text
    assert "iss=https://accounts.example" in text and "scope=read" in text
    assert text.startswith("http://127.0.0.1:39847/callback?state=" + REDACTED)
    assert count == 2


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("CLOCKWORK_WEB_SECRET=hunter2hunter2", "CLOCKWORK_WEB_SECRET="),
        ("export API_TOKEN: abcdefghijkl", "export API_TOKEN: "),
        ('{"refresh_token": "abcdefghijklmnop"}', '{"refresh_token": "'),
        ("password = 'correct-horse-battery'", "password = '"),
    ],
)
def test_named_secret_fields_keep_their_name_and_lose_the_value(line, kept):
    text, count = redact(line)
    assert text.startswith(kept + REDACTED)
    assert count == 1


def test_authorization_headers_and_url_credentials_are_masked():
    text, count = redact(
        "Authorization: Bearer abcdefghijklmnop12345 and https://admin:s3cretpass@example.test/x"
    )
    assert "abcdefghijklmnop12345" not in text and "s3cretpass" not in text
    assert "https://admin:" + REDACTED + "@example.test/x" in text
    assert count == 2


def test_private_key_blocks_are_masked_even_when_truncated():
    block = "-----BEGIN " + "PRIVATE KEY-----\nMIIBVgIBADANBgkq\nabcdef\n"
    text, count = redact("before\n" + block + "-----END PRIVATE KEY-----\nafter")
    assert "MIIBVg" not in text and text.startswith("before\n") and text.endswith("\nafter")
    cut, _ = redact("before\n" + block)  # cut off mid-key by a truncated transcript
    assert "MIIBVg" not in cut
    assert count == 1


@pytest.mark.parametrize(
    "plain",
    [
        "exit code=1 and state=ok",  # not URL parameters
        "max_tokens=1000 token: none",  # short values are not secrets
        "see https://example.test/page?x=1&y=2",
        "path /home/user/project/src/file.py",
        "",
    ],
)
def test_ordinary_text_is_untouched(plain):
    assert redact(plain) == (plain, 0)


def test_redaction_is_idempotent():
    once, _ = redact(f"token: {GOOGLE_ACCESS} and ?code={GOOGLE_CODE}")
    assert redact(once) == (once, 0)


def test_brief_masks_secrets_before_truncating_and_says_so(app_config):
    path = seed_codex(app_config.codex_root)
    secret = GOOGLE_REFRESH
    padding = "x" * (handoff.MAX_TURN_CHARS - 20)
    write_jsonl(
        path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            # The secret straddles the truncation point.
                            "text": f"{padding} refresh {secret} tail",
                        }
                    ],
                },
            }
        ],
    )
    from session_control.scanner import SessionScanner

    session = {s.provider: s for s in SessionScanner(app_config).scan().sessions}["codex"]
    brief = handoff.build_brief(session, handoff.recent_turns(session))
    assert secret not in brief and secret[:12] not in brief
    assert REDACTED in brief
    assert "1 secret-like value(s)" in brief
