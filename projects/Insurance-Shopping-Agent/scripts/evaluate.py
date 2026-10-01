"""Run the labeled evaluation set against the rules-only workflow and the (scripted) agent.

Usage: python scripts/evaluate.py [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.evaluation import EvalRun, build_context, summarize  # noqa: E402
from app.fixtures import load_eval_cases  # noqa: E402


async def run_mode(via_agent: bool):
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for case in load_eval_cases():
            if via_agent and (case.get("scenario") or case["category"] not in ("ordinary_completion", "refusal") or case["needs_overrides"].get("state_code")):
                continue
            d = Path(tmp) / case["id"]
            d.mkdir()
            ctx = build_context(str(d))
            results.append(await EvalRun(ctx, via_agent=via_agent).run(case))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=None, help="write full results to this file")
    args = parser.parse_args()
    rules = asyncio.run(run_mode(False))
    agent = asyncio.run(run_mode(True))
    report = {"rules_only": summarize(rules), "agent_scripted": summarize(agent), "fixture_version": "2026.09.1"}
    print("Rules-only workflow:  %(passed)d/%(total)d passed (held-out %(held_out_passed)d/%(held_out_total)d); unsupported claims %(unsupported_claims_total)d; "
          "unnecessary questions %(unnecessary_questions_total)d; provider calls %(provider_calls_total)d; duplicate submits %(duplicate_submits)d" % report["rules_only"])
    print("Agent (scripted):     %(passed)d/%(total)d passed; tool runs %(tool_runs_total)d; provider calls %(provider_calls_total)d; unnecessary questions %(unnecessary_questions_total)d" % report["agent_scripted"])
    for cat, v in sorted(report["rules_only"]["by_category"].items()):
        print("  %-22s %d/%d" % (cat, v["passed"], v["total"]))
    failed = [r for r in rules + agent if not r["passed"]]
    for r in failed:
        print("FAILED", r["id"], json.dumps(r["mismatches"], default=str))
    if args.json:
        Path(args.json).write_text(json.dumps({"report": report, "rules_only": rules, "agent_scripted": agent}, indent=2, default=str))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
