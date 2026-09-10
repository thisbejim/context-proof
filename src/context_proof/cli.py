"""Command-line interface for :mod:`context_proof`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .core import (
    CaseError,
    Report,
    analyze_case,
    explain_checks,
    load_case,
    render_markdown,
    render_text,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context-proof",
        description="Prove that declared agent context invariants survive compaction.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check a JSON or phase-tagged JSONL transition")
    check.add_argument(
        "path",
        nargs="?",
        default="-",
        help="case path, or - for stdin (default: -)",
    )
    check.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="report format (default: text)",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    check.add_argument(
        "--output",
        type=Path,
        help="write the report to a file instead of stdout",
    )

    commands.add_parser("explain", help="list check codes and their meanings")
    return parser


def _render(report: Report, output_format: str, strict: bool) -> str:
    if output_format == "json":
        # ``report`` is a Report here; keeping this helper small makes the output
        # path easy to exercise from tests without duplicating serialization logic.
        return json.dumps(
            report.to_dict(strict=strict), ensure_ascii=False, indent=2, sort_keys=True
        )
    if output_format == "markdown":
        return render_markdown(report, strict=strict)
    return render_text(report, strict=strict)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    args = _parser().parse_args(argv)
    if args.command == "explain":
        print(explain_checks())
        return 0

    try:
        case = load_case(args.path)
        report = analyze_case(case)
        rendered = _render(report, args.format, args.strict)
        if args.output is None:
            print(rendered)
        else:
            args.output.write_text(rendered + "\n", encoding="utf-8")
    except (CaseError, OSError, TypeError, ValueError) as exc:
        print(f"context-proof: {exc}", file=sys.stderr)
        return 2
    return report.exit_code(strict=args.strict)


__all__ = ["main"]
