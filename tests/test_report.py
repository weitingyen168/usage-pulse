"""Offline unit tests for the report pipeline. No network, no external data."""
from __future__ import annotations

from usage_pulse.report import (
    aggregate,
    clean,
    detect_changes,
    render_report,
)
from usage_pulse.generate_data import generate


def _row(date, workflow, source, sessions, completed, accepted, avg_rating=""):
    return {
        "date": date,
        "workflow": workflow,
        "source": source,
        "sessions": str(sessions),
        "completed": str(completed),
        "accepted": str(accepted),
        "avg_rating": str(avg_rating),
    }


def test_clean_normalizes_workflow_casing_and_whitespace():
    rows = [_row("2026-08-01", "  Reply-Draft  ", "app", 100, 90, 70, 4.2)]
    result = clean(rows)
    assert len(result.events) == 1
    assert result.events[0].workflow == "reply-draft"


def test_clean_drops_exact_duplicates():
    r = _row("2026-08-01", "search", "app", 100, 98, 60, 3.9)
    result = clean([r, dict(r)])
    assert len(result.events) == 1
    assert any("duplicate" in i for i in result.issues)


def test_clean_excludes_demo_traffic_but_reports_it():
    rows = [
        _row("2026-08-01", "search", "app", 100, 98, 60, 3.9),
        _row("2026-08-01", "search", "demo", 900, 890, 860, 4.9),
    ]
    result = clean(rows)
    assert len(result.events) == 1
    assert result.excluded == 1
    assert any("demo" in i for i in result.issues)


def test_clean_rejects_impossible_counts():
    rows = [_row("2026-08-01", "search", "app", 10, 20, 5)]  # completed > sessions
    result = clean(rows)
    assert result.events == []
    assert any("impossible" in i for i in result.issues)


def test_clean_handles_na_rating():
    rows = [_row("2026-08-01", "search", "app", 100, 98, 60, "n/a")]
    result = clean(rows)
    assert result.events[0].avg_rating is None


def test_aggregate_is_session_weighted():
    # Day 1: tiny sample at 100% acceptance; Day 2: large sample at 50%.
    rows = [
        _row("2026-08-01", "search", "app", 10, 10, 10, 5.0),
        _row("2026-08-02", "search", "app", 1000, 1000, 500, 3.0),
    ]
    health = aggregate(clean(rows).events)
    assert len(health) == 1
    # Session-weighted acceptance = 510 / 1010 ~= 0.505, NOT the naive 0.75.
    assert abs(health[0].acceptance_rate - (510 / 1010)) < 1e-9


def test_aggregate_sorted_by_acceptance_desc():
    rows = [
        _row("2026-08-01", "low", "app", 100, 100, 40, 3.0),
        _row("2026-08-01", "high", "app", 100, 100, 90, 4.5),
    ]
    health = aggregate(clean(rows).events)
    assert [w.workflow for w in health] == ["high", "low"]


def test_rating_coverage_reflects_missing_ratings():
    rows = [
        _row("2026-08-01", "search", "app", 100, 100, 60, 4.0),
        _row("2026-08-02", "search", "app", 100, 100, 60, "n/a"),
    ]
    health = aggregate(clean(rows).events)
    assert abs(health[0].rating_coverage - 0.5) < 1e-9


def test_detect_changes_flags_a_drop():
    rows = [
        _row("2026-08-01", "reply-draft", "app", 100, 100, 80, 4.3),
        _row("2026-08-02", "reply-draft", "app", 100, 100, 78, 4.3),
        _row("2026-08-03", "reply-draft", "app", 100, 100, 45, 3.5),
        _row("2026-08-04", "reply-draft", "app", 100, 100, 44, 3.5),
    ]
    changes = detect_changes(clean(rows).events)
    assert len(changes) == 1
    assert changes[0].workflow == "reply-draft"
    assert changes[0].drop > 0.15


def test_detect_changes_ignores_stable_workflows():
    rows = [
        _row("2026-08-01", "summarize", "app", 100, 100, 80, 4.5),
        _row("2026-08-02", "summarize", "app", 100, 100, 81, 4.5),
        _row("2026-08-03", "summarize", "app", 100, 100, 80, 4.5),
        _row("2026-08-04", "summarize", "app", 100, 100, 79, 4.5),
    ]
    assert detect_changes(clean(rows).events) == []


def test_generated_data_produces_a_full_report():
    rows = generate(seed=7, days=14)
    result = clean(rows)
    report = render_report(result)
    # The generator injects a demo spike, a duplicate, casing issues, and a
    # reply-draft regression — all should surface.
    assert result.excluded >= 1
    assert "duplicate" in report
    assert "reply-draft" in report
    assert "WEEKLY PRODUCT-USAGE HEALTH CHECK" in report
