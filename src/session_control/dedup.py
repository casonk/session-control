"""Duplicate session detection for session-control."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from session_control.models import SessionRecord

DEFAULT_SIMILARITY = 0.72


def find_duplicate_groups(
    sessions: tuple[SessionRecord, ...],
    min_age: timedelta,
    max_age: timedelta,
    similarity: float = DEFAULT_SIMILARITY,
    now: datetime | None = None,
) -> list[list[SessionRecord]]:
    """Return groups of 2+ sessions that appear to be duplicates of each other.

    Sessions are candidates only if their age falls in [min_age, max_age].
    Grouping is restricted to the same provider (cross-provider merges are not
    meaningful). Within each provider, sessions are clustered by fuzzy similarity
    of their preview text (first user prompt).
    """
    reference = now or datetime.now(timezone.utc)
    in_window = [s for s in sessions if _in_age_window(s, min_age, max_age, reference)]

    # Group by (provider, workspace) — sessions in different workspaces are never merged.
    by_bucket: dict[tuple[str, str], list[SessionRecord]] = {}
    for s in in_window:
        key = (s.provider, s.workspace or "")
        by_bucket.setdefault(key, []).append(s)

    groups: list[list[SessionRecord]] = []
    for bucket_sessions in by_bucket.values():
        groups.extend(_cluster(bucket_sessions, similarity))
    return groups


def session_similarity(a: SessionRecord, b: SessionRecord) -> float:
    """Return a [0, 1] similarity score between two sessions based on preview text."""
    text_a = _canonical(a.preview or a.title)
    text_b = _canonical(b.preview or b.title)
    if not text_a or not text_b:
        return 0.0
    return SequenceMatcher(None, text_a, text_b).ratio()


def _cluster(sessions: list[SessionRecord], threshold: float) -> list[list[SessionRecord]]:
    """Greedy single-linkage clustering: two sessions join a group when their
    similarity to any current member exceeds the threshold."""
    groups: list[list[SessionRecord]] = []
    assigned = [False] * len(sessions)

    for i, session in enumerate(sessions):
        if assigned[i]:
            continue
        group = [session]
        assigned[i] = True
        for j in range(i + 1, len(sessions)):
            if assigned[j]:
                continue
            if any(session_similarity(sessions[j], member) >= threshold for member in group):
                group.append(sessions[j])
                assigned[j] = True
        if len(group) >= 2:
            groups.append(group)

    return groups


def _in_age_window(
    session: SessionRecord,
    min_age: timedelta,
    max_age: timedelta,
    now: datetime,
) -> bool:
    raw = session.updated_at or session.created_at
    if not raw:
        return False
    parsed = _parse_datetime(raw)
    if parsed is None:
        return False
    age = now - parsed
    return min_age <= age <= max_age


def _canonical(text: str) -> str:
    return " ".join((text or "").lower().split())


def _parse_datetime(value: str) -> datetime | None:
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
