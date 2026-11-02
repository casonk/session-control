"""Duplicate detection: which sessions are considered the same conversation.

This decides what ``deduplicate`` merges and moves to trash, so the edges matter: the age
window, provider and workspace boundaries, and the similarity threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_control import dedup
from session_control.dedup import DEFAULT_SIMILARITY, find_duplicate_groups, session_similarity
from session_control.models import SessionRecord

NOW = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def rec(
    sid: str = "a",
    *,
    provider: str = "claude",
    workspace: str = "/w/one",
    preview: str = "Fix the statement downloader",
    title: str = "",
    updated: str | None = "2026-04-08T12:00:00Z",  # two days before NOW
    created: str = "2026-04-01T12:00:00Z",
) -> SessionRecord:
    path = Path(f"/nonexistent/{sid}.jsonl")
    return SessionRecord(
        provider=provider,
        session_id=sid,
        title=title,
        workspace=workspace,
        created_at=created,
        updated_at=updated or "",
        primary_path=path,
        delete_targets=(path,),
        size_bytes=1,
        message_count=1,
        preview=preview,
        resume_command="",
    )


def groups(*sessions: SessionRecord, **kwargs) -> list[list[str]]:
    kwargs.setdefault("min_age", DAY)
    kwargs.setdefault("max_age", 30 * DAY)
    kwargs.setdefault("now", NOW)
    found = find_duplicate_groups(tuple(sessions), **kwargs)
    return sorted(sorted(s.session_id for s in g) for g in found)


class TestSimilarity:
    def test_identical_previews_score_one(self):
        assert session_similarity(rec("a"), rec("b")) == 1.0

    def test_case_and_whitespace_do_not_matter(self):
        a = rec("a", preview="Fix the   Statement\nDownloader")
        b = rec("b", preview="fix the statement downloader")
        assert session_similarity(a, b) == 1.0

    def test_the_title_stands_in_for_a_missing_preview(self):
        a = rec("a", preview="", title="Refactor the scanner")
        b = rec("b", preview="Refactor the scanner")
        assert session_similarity(a, b) == 1.0

    def test_a_session_with_no_text_matches_nothing_not_even_itself(self):
        blank = rec("a", preview="", title="")
        assert session_similarity(blank, rec("b")) == 0.0
        assert session_similarity(blank, blank) == 0.0

    def test_unrelated_text_scores_low(self):
        assert session_similarity(rec("a"), rec("b", preview="Plan the garden layout")) < 0.4


class TestAgeWindow:
    def test_only_sessions_inside_the_window_are_candidates(self):
        old = rec("old", updated="2026-02-01T00:00:00Z")  # ~68 days
        recent = rec("recent", updated="2026-04-10T11:00:00Z")  # one hour
        inside_a, inside_b = rec("in-a"), rec("in-b")
        assert groups(old, recent, inside_a, inside_b) == [["in-a", "in-b"]]

    def test_both_bounds_are_inclusive(self):
        at_min = rec("min", updated=(NOW - DAY).isoformat())
        at_max = rec("max", updated=(NOW - 30 * DAY).isoformat())
        assert groups(at_min, at_max) == [["max", "min"]]
        just_young = rec("young", updated=(NOW - DAY + timedelta(seconds=1)).isoformat())
        just_old = rec("older", updated=(NOW - 30 * DAY - timedelta(seconds=1)).isoformat())
        assert groups(just_young, just_old, rec("x")) == []

    def test_updated_at_wins_over_created_at_and_created_at_is_the_fallback(self):
        # created long ago but updated recently: outside a 1d-30d window by its update time
        recently_touched = rec(
            "touched", created="2026-01-01T00:00:00Z", updated="2026-04-10T11:59:00Z"
        )
        assert groups(recently_touched, rec("b")) == []
        # no update time: the creation time decides
        a = rec("a", updated=None, created="2026-04-05T00:00:00Z")
        b = rec("b", updated=None, created="2026-04-06T00:00:00Z")
        assert groups(a, b) == [["a", "b"]]

    def test_sessions_with_no_usable_time_are_never_candidates(self):
        no_time = rec("n1", updated=None, created="")
        garbage = rec("n2", updated="not a date")
        assert groups(no_time, garbage, rec("b")) == []

    def test_z_suffix_offsets_and_naive_times_are_all_read_as_utc(self):
        z = rec("z", updated="2026-04-08T12:00:00Z")
        offset = rec("o", updated="2026-04-08T14:00:00+02:00")
        naive = rec("n", updated="2026-04-08T12:00:00")
        assert groups(z, offset, naive) == [["n", "o", "z"]]

    def test_without_now_the_current_time_is_used(self):
        stamp = (datetime.now(timezone.utc) - 2 * DAY).isoformat()
        a, b = rec("a", updated=stamp), rec("b", updated=stamp)
        assert groups(a, b, now=None) == [["a", "b"]]


class TestBoundaries:
    def test_sessions_in_different_providers_are_never_merged(self):
        assert groups(rec("a", provider="claude"), rec("b", provider="codex")) == []

    def test_sessions_in_different_workspaces_are_never_merged(self):
        assert groups(rec("a", workspace="/w/one"), rec("b", workspace="/w/two")) == []

    def test_a_missing_workspace_is_its_own_bucket(self):
        assert groups(rec("a", workspace=""), rec("b", workspace="")) == [["a", "b"]]
        assert groups(rec("a", workspace=""), rec("b", workspace="/w/one")) == []

    def test_a_lone_session_is_not_a_group(self):
        assert groups(rec("a")) == []
        assert groups() == []


class TestClustering:
    def test_similar_sessions_group_and_dissimilar_ones_stay_out(self):
        a, b = rec("a"), rec("b", preview="Fix the statement downloader please")
        c = rec("c", preview="Plan the garden layout")
        assert groups(a, b, c) == [["a", "b"]]

    def test_two_separate_conversations_make_two_groups(self):
        pairs = [
            rec("a1"),
            rec("a2"),
            rec("b1", preview="Plan the garden layout"),
            rec("b2", preview="Plan the garden layout"),
        ]
        assert groups(*pairs) == [["a1", "a2"], ["b1", "b2"]]

    def test_similarity_chains_through_a_middle_session(self):
        # a~b and b~c clear the bar, but a~c does not: single linkage still groups all three
        a = rec("a", preview="aaaaaaaabbbbbbbbbb")
        b = rec("b", preview="aaaaaaaabbbbbbbbbbcccccccccc")
        c = rec("c", preview="bbbbbbbbbbcccccccccc")
        assert session_similarity(a, c) < 0.6 <= session_similarity(a, b)
        assert session_similarity(b, c) >= 0.6
        assert groups(a, b, c, similarity=0.6) == [["a", "b", "c"]]

    def test_the_threshold_is_inclusive_and_adjustable(self):
        a, b = rec("a"), rec("b", preview="Fix the statement downloader now")
        score = session_similarity(a, b)
        assert groups(a, b, similarity=score) == [["a", "b"]]
        assert groups(a, b, similarity=score + 0.001) == []
        assert groups(a, b, similarity=0.0) == [["a", "b"]]

    def test_the_default_threshold_is_the_documented_one(self):
        assert DEFAULT_SIMILARITY == 0.72
        near = rec("b", preview="Fix the statement downloader")  # identical: above any threshold
        assert groups(rec("a"), near) == [["a", "b"]]

    def test_a_session_claimed_by_one_group_cannot_also_join_another(self):
        # a and b do not match each other (0.40), but both match c (0.77). c goes to the
        # first group that claims it; it must not also be pulled into a second one, or the
        # same session would be merged twice.
        a = rec("a", preview="alpha beta")
        b = rec("b", preview="beta gamma")
        c = rec("c", preview="alpha beta gamma")
        assert session_similarity(a, b) < DEFAULT_SIMILARITY
        assert session_similarity(a, c) >= DEFAULT_SIMILARITY
        assert session_similarity(b, c) >= DEFAULT_SIMILARITY
        assert groups(a, b, c) == [["a", "c"]]

    def test_every_session_ends_up_in_at_most_one_group(self):
        many = [rec(f"s{i}") for i in range(6)]
        found = find_duplicate_groups(tuple(many), DAY, 30 * DAY, now=NOW)
        ids = [s.session_id for g in found for s in g]
        assert len(ids) == len(set(ids)) == 6


class TestParsing:
    def test_parse_datetime_handles_the_forms_providers_write(self):
        assert dedup._parse_datetime("2026-04-08T12:00:00Z") == datetime(
            2026, 4, 8, 12, tzinfo=timezone.utc
        )
        assert dedup._parse_datetime("  2026-04-08T12:00:00+00:00  ") == datetime(
            2026, 4, 8, 12, tzinfo=timezone.utc
        )
        assert dedup._parse_datetime("2026-04-08T14:00:00+02:00") == datetime(
            2026, 4, 8, 12, tzinfo=timezone.utc
        )
        assert dedup._parse_datetime("2026-04-08T12:00:00").tzinfo is not None
        assert dedup._parse_datetime("yesterday") is None
        assert dedup._parse_datetime("") is None
