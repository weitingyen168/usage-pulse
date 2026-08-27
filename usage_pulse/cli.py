"""Command-line entry point for usage-pulse.

Usage:
    python -m usage_pulse.cli --input data/sample_events.csv
"""
from __future__ import annotations

import argparse
import sys

from usage_pulse.report import clean, load_csv, render_report


def main(argv: list[str] | None = None) -> int:
    # Ensure report output works regardless of the terminal's locale encoding
    # (e.g. cp950/cp1252 on Windows) rather than crashing on non-ASCII data.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="usage-pulse",
        description="Turn a product-usage events CSV into a weekly health report.",
    )
    parser.add_argument(
        "--input",
        "-i",
        default="data/sample_events.csv",
        help="Path to the events CSV (default: data/sample_events.csv).",
    )
    args = parser.parse_args(argv)

    try:
        rows = load_csv(args.input)
    except FileNotFoundError:
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 1

    print(render_report(clean(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
