"""Repeatable rules-only evaluation, with separate development and held-out results."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
from app.workflows.engine import Engine
from test_evaluation import CASES, run_case


def main():
    results = []
    for case in CASES:
        with TemporaryDirectory() as d:
            result = run_case(Engine(d), case)
        results.append({"id": case["id"], "split": case["split"], "expected_status": case["expected_status"], "actual_status": result["status"], "passed": result["status"] == case["expected_status"], "tool_calls": result["tool_calls"], "cost_minor": result["cost_minor"]})
    report = {"fixture_version": "2026.09.25-v1", "baseline": "rules-only-v1", "external_model_evaluated": False, "cases": len(results), "passed": sum(r["passed"] for r in results), "held_out": {"cases": sum(r["split"] == "held_out" for r in results), "passed": sum(r["split"] == "held_out" and r["passed"] for r in results)}, "model_cost_usd": 0, "results": results}
    path = Path(__file__).parents[1] / "docs/evaluation-report.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['passed']}/{report['cases']} labeled cases passed; held-out {report['held_out']['passed']}/{report['held_out']['cases']}. Report: {path}")
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
