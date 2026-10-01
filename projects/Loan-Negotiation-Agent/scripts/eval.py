"""Run the labelled agent evaluation and write a report.

Usage: python scripts/eval.py [--out docs/eval-report.json] [--no-held-out]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.agent.evaluation import EvalRunner  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/eval-report.json")
    parser.add_argument("--no-held-out", action="store_true")
    args = parser.parse_args()
    runner = EvalRunner()
    report = runner.run_all(include_held_out=not args.no_held_out)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    failed = [r for r in report["results"] if not r["completed"]]
    for r in failed:
        print("FAILED", r["executor"], r["case_id"], r["failures"])
    print(f"report written to {args.out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
