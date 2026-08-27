"""Core logic for the weekly product-usage health report.

Pure and dependency-free (standard library only). Every function here is
deterministic and unit-tested, so the CLI (`usage_pulse/cli.py`) can stay a thin
wrapper around it.

The pipeline is:  clean() -> aggregate() -> detect_changes() -> render_report()
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Rows whose `source` is one of these are shown in the data-quality summary but
# excluded from the health metrics (e.g. internal demo traffic that would skew
# the numbers).
EXCLUDED_SOURCES = {"demo", "test", "internal"}

# A workflow's acceptance rate dropping by at least this many percentage points
# from the first half to the second half of the window is flagged as a change
# event worth investigating.
CHANGE_THRESHOLD = 0.15


@dataclass(frozen=True)
class CleanEvent:
    date: str
    workflow: str
    source: str
    sessions: int
    completed: int
    accepted: int
    avg_rating: float | None


@dataclass
class CleanResult:
    events: list[CleanEvent]   # trustworthy rows used for metrics
    issues: list[str]          # human-readable data-quality notes
    excluded: int              # count of rows set aside (not used in metrics)


@dataclass(frozen=True)
class WorkflowHealth:
    workflow: str
    sessions: int
    completion_rate: float     # completed / sessions   (session-weighted)
    acceptance_rate: float     # accepted / completed    (session-weighted)
    avg_rating: float | None   # session-weighted mean rating, if any
    rating_coverage: float     # fraction of sessions that carried a rating


@dataclass(frozen=True)
class ChangeEvent:
    workflow: str
    early_acceptance: float
    late_acceptance: float
    drop: float                # early - late, in rate points


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
_MISSING = {"", "n/a", "na", "null", "none"}


def _to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is a subclass of int
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip().lower()
    if value in _MISSING:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip().lower()
    if value in _MISSING:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _norm_workflow(value) -> str:
    return str(value or "").strip().lower()


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #
def clean(rows: Iterable[dict]) -> CleanResult:
    """Normalize raw CSV rows into trustworthy `CleanEvent`s.

    Handles: inconsistent workflow casing, non-numeric placeholders ("n/a"),
    exact duplicate rows, impossible counts (accepted > completed > sessions),
    zero-session rows, and excluded traffic sources. Every decision is recorded
    in `issues` so the report can show its work rather than silently dropping
    data.
    """
    events: list[CleanEvent] = []
    issues: list[str] = []
    excluded = 0
    seen: set[tuple] = set()

    for i, row in enumerate(rows, start=1):
        workflow = _norm_workflow(row.get("workflow"))
        source = (row.get("source") or "").strip().lower()
        sessions = _to_int(row.get("sessions"))
        completed = _to_int(row.get("completed"))
        accepted = _to_int(row.get("accepted"))
        rating = _to_float(row.get("avg_rating"))
        date = (row.get("date") or "").strip()

        key = (date, workflow, source, sessions, completed, accepted)
        if key in seen:
            issues.append(f"row {i}: duplicate row for {date}/{workflow} - dropped")
            continue
        seen.add(key)

        if not workflow or sessions is None or completed is None or accepted is None:
            issues.append(f"row {i}: missing workflow/counts - dropped")
            continue

        if sessions <= 0:
            issues.append(f"row {i}: {date}/{workflow} has 0 sessions - dropped")
            continue

        if not (accepted <= completed <= sessions):
            issues.append(
                f"row {i}: {date}/{workflow} has impossible counts "
                f"(sessions={sessions}, completed={completed}, accepted={accepted}) - dropped"
            )
            continue

        if source in EXCLUDED_SOURCES:
            excluded += 1
            issues.append(
                f"row {i}: {date}/{workflow} is '{source}' traffic ({sessions} sessions) "
                f"- excluded from metrics but shown here"
            )
            continue

        events.append(
            CleanEvent(date, workflow, source, sessions, completed, accepted, rating)
        )

    return CleanResult(events=events, issues=issues, excluded=excluded)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def aggregate(events: list[CleanEvent]) -> list[WorkflowHealth]:
    """Session-weighted health per workflow, sorted by acceptance rate desc.

    Session-weighting matters: averaging a 5-session day and a 500-session day
    equally would let a tiny, noisy day swing the headline number.
    """
    by_workflow: dict[str, list[CleanEvent]] = {}
    for e in events:
        by_workflow.setdefault(e.workflow, []).append(e)

    results: list[WorkflowHealth] = []
    for workflow, evs in by_workflow.items():
        sessions = sum(e.sessions for e in evs)
        completed = sum(e.completed for e in evs)
        accepted = sum(e.accepted for e in evs)

        rated_sessions = sum(e.sessions for e in evs if e.avg_rating is not None)
        weighted_rating_sum = sum(
            e.avg_rating * e.sessions for e in evs if e.avg_rating is not None
        )

        results.append(
            WorkflowHealth(
                workflow=workflow,
                sessions=sessions,
                completion_rate=(completed / sessions) if sessions else 0.0,
                acceptance_rate=(accepted / completed) if completed else 0.0,
                avg_rating=(weighted_rating_sum / rated_sessions) if rated_sessions else None,
                rating_coverage=(rated_sessions / sessions) if sessions else 0.0,
            )
        )

    results.sort(key=lambda w: w.acceptance_rate, reverse=True)
    return results


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #
def detect_changes(
    events: list[CleanEvent], threshold: float = CHANGE_THRESHOLD
) -> list[ChangeEvent]:
    """Flag workflows whose acceptance rate fell sharply across the window.

    Splits each workflow's dates into an early and late half (by sorted unique
    date) and compares session-weighted acceptance. This is intentionally
    simple - a signal to investigate, not a statistical test.
    """
    by_workflow: dict[str, list[CleanEvent]] = {}
    for e in events:
        by_workflow.setdefault(e.workflow, []).append(e)

    changes: list[ChangeEvent] = []
    for workflow, evs in by_workflow.items():
        dates = sorted({e.date for e in evs})
        if len(dates) < 2:
            continue
        mid = len(dates) // 2
        early_dates = set(dates[:mid])
        late_dates = set(dates[mid:])

        def _acceptance(selected: set[str]) -> float:
            comp = sum(e.completed for e in evs if e.date in selected)
            acc = sum(e.accepted for e in evs if e.date in selected)
            return (acc / comp) if comp else 0.0

        early = _acceptance(early_dates)
        late = _acceptance(late_dates)
        drop = early - late
        if drop >= threshold:
            changes.append(ChangeEvent(workflow, early, late, drop))

    changes.sort(key=lambda c: c.drop, reverse=True)
    return changes


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def render_report(clean_result: CleanResult) -> str:
    events = clean_result.events
    health = aggregate(events)
    changes = detect_changes(events)

    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("WEEKLY PRODUCT-USAGE HEALTH CHECK")
    lines.append("=" * 64)

    # 1. Data quality
    lines.append("\n[1] Data quality")
    if clean_result.issues:
        for issue in clean_result.issues:
            lines.append(f"  - {issue}")
    else:
        lines.append("  - no issues detected")
    lines.append(
        f"  ({len(events)} rows used, {clean_result.excluded} excluded from metrics)"
    )

    # 2. Health table
    lines.append("\n[2] Health by workflow (session-weighted)")
    lines.append(
        f"  {'workflow':<16}{'sessions':>10}{'completion':>12}"
        f"{'acceptance':>12}{'rating':>9}"
    )
    for w in health:
        rating = f"{w.avg_rating:.2f}" if w.avg_rating is not None else "  n/a"
        lines.append(
            f"  {w.workflow:<16}{w.sessions:>10}{_pct(w.completion_rate):>12}"
            f"{_pct(w.acceptance_rate):>12}{rating:>9}"
        )

    # 3. What looks best
    lines.append("\n[3] Looking best right now")
    if health:
        best = health[0]
        lines.append(
            f"  {best.workflow} - {_pct(best.acceptance_rate).strip()} acceptance "
            f"over {best.sessions} sessions"
        )
    else:
        lines.append("  - no trustworthy data")

    # 4. Trust-least metric
    lines.append("\n[4] Trust this metric least")
    low_cov = [w for w in health if w.rating_coverage < 0.5]
    if low_cov:
        worst = min(low_cov, key=lambda w: w.rating_coverage)
        lines.append(
            f"  avg_rating for '{worst.workflow}': only "
            f"{_pct(worst.rating_coverage).strip()} of sessions carried a rating"
        )
    else:
        lines.append("  - rating coverage is healthy across workflows")

    # 5. Changes to investigate
    lines.append("\n[5] Change events to investigate")
    if changes:
        for c in changes:
            lines.append(
                f"  {c.workflow}: acceptance {_pct(c.early_acceptance).strip()} "
                f"-> {_pct(c.late_acceptance).strip()} "
                f"(down {c.drop * 100:.1f} pts across the window)"
            )
    else:
        lines.append("  - no sharp week-over-week drops")

    lines.append("")
    return "\n".join(lines)


def load_csv(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
