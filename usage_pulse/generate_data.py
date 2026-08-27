"""Deterministic synthetic data generator for usage-pulse.

Produces a product-usage events CSV with realistic, *intentional* data-quality
problems (inconsistent casing, an 'n/a' rating, a duplicate export row, and a
demo-traffic spike) plus a mid-window acceptance regression on one workflow —
so the report has something meaningful to say out of the box.

    python -m usage_pulse.generate_data --out data/sample_events.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta

FIELDS = ["date", "workflow", "source", "sessions", "completed", "accepted", "avg_rating"]

WORKFLOWS = {
    # workflow: (base_sessions, completion_rate, acceptance_rate, rating)
    "reply-draft": (120, 0.90, 0.72, 4.3),
    "summarize": (80, 0.95, 0.81, 4.5),
    "search": (200, 0.98, 0.60, 3.9),
}

# reply-draft regresses after a policy change midway through the window.
REGRESSION_WORKFLOW = "reply-draft"
REGRESSION_START_DAY = 7
REGRESSION_ACCEPTANCE = 0.48


def _rows(rng: random.Random, start: date, days: int) -> list[dict]:
    rows: list[dict] = []
    for d in range(days):
        day = start + timedelta(days=d)
        for workflow, (base, comp_rate, acc_rate, rating) in WORKFLOWS.items():
            sessions = max(1, int(rng.gauss(base, base * 0.12)))
            completed = int(sessions * min(1.0, rng.gauss(comp_rate, 0.02)))

            eff_acc = acc_rate
            if workflow == REGRESSION_WORKFLOW and d >= REGRESSION_START_DAY:
                eff_acc = REGRESSION_ACCEPTANCE
            accepted = int(completed * min(1.0, rng.gauss(eff_acc, 0.02)))

            rows.append(
                {
                    "date": day.isoformat(),
                    "workflow": workflow,
                    "source": "app",
                    "sessions": sessions,
                    "completed": completed,
                    "accepted": accepted,
                    "avg_rating": round(rng.gauss(rating, 0.1), 2),
                }
            )
    return rows


def _inject_issues(rows: list[dict], start: date) -> list[dict]:
    # Inconsistent casing / whitespace on some workflow labels.
    for r in rows[:3]:
        r["workflow"] = r["workflow"].upper()
    if rows:
        rows[4]["workflow"] = f"  {rows[4]['workflow']}  "

    # A non-numeric rating placeholder.
    if len(rows) > 6:
        rows[6]["avg_rating"] = "n/a"

    # A duplicate export row.
    if rows:
        rows.append(dict(rows[0]))

    # A demo-account traffic spike that should be excluded from metrics.
    rows.append(
        {
            "date": (start + timedelta(days=5)).isoformat(),
            "workflow": "search",
            "source": "demo",
            "sessions": 900,
            "completed": 890,
            "accepted": 860,
            "avg_rating": 4.9,
        }
    )
    return rows


def generate(seed: int = 7, days: int = 14) -> list[dict]:
    rng = random.Random(seed)
    start = date(2026, 8, 1)
    rows = _rows(rng, start, days)
    return _inject_issues(rows, start)


def write_csv(rows: list[dict], out: str) -> None:
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic usage events.")
    parser.add_argument("--out", "-o", default="data/sample_events.csv")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)

    rows = generate(seed=args.seed, days=args.days)
    write_csv(rows, args.out)
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
