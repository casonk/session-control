"""The scheduled-job templates must keep their command sequence and journal identity."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "config" / "clockwork"


def _text(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _value(text: str, key: str) -> str:
    match = re.search(rf'^{key} = "(.*)"$', text, re.M)
    assert match, key
    return match.group(1)


def test_prune_template_deduplicates_then_prunes():
    text = _text("prune-sessions.toml.template")
    assert "session-control deduplicate --min-age 50d --max-age __OLDER_THAN__" in _value(
        text, "exec_start"
    )
    assert "session-control prune --older-than __OLDER_THAN__" in _value(text, "exec_start_then")
    cron = _value(text, "command")
    assert cron.index("deduplicate") < cron.index(" prune ") and "&&" in cron


def test_timer_templates_share_one_journal_identity():
    for name in ("prune-sessions.toml.template", "condense-sessions.toml.template"):
        assert _value(_text(name), "syslog_identifier") == "session-control"
