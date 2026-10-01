"""Run the labeled agent evaluation and write results.

Usage (from backend/):
    python scripts/run_eval.py                 # rules-only policy (baseline)
    python scripts/run_eval.py --policy agent  # configured model policy (falls back to rules)
    python scripts/run_eval.py --split heldout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PB_SERVE_FRONTEND", "false")

from app.agent.evaluation import run_all  # noqa: E402
from app.config import FIXTURES_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", choices=["rules", "agent"], default="rules")
    parser.add_argument("--split", choices=["dev", "heldout"], default=None)
    parser.add_argument("--out", default=str(FIXTURES_DIR / "eval_results.json"))
    args = parser.parse_args()

    report = run_all(args.policy, args.split)
    Path(args.out).write_text(json.dumps(report, indent=2))
    s = report["summary"]
    print(f"policy={report['policy']} model={report['model_version']} fixture={report['fixture_version']}")
    for name in ("all", "dev", "heldout"):
        b = s[name]
        if b["cases"]:
            print(f"{name:8s} cases={b['cases']:2d} completed={b['completed']:2d} ({b['completion_rate']:.0%}) unnecessary_questions={b['unnecessary_questions']} unsupported_claims={b['unsupported_claims']} tool_calls/case={b['tool_calls_per_case']}")
    for cat, b in s["by_category"].items():
        print(f"  {cat:22s} {b['completed']}/{b['cases']}")
    if s["failed_cases"]:
        print("failed:")
        for f in s["failed_cases"]:
            print(" ", f["id"], f["failures"])
    print(f"written {args.out}")
    return 0 if not s["failed_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
