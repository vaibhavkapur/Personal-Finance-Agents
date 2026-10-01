"""Run every labeled evaluation case through the rules-only workflow and (for a subset) the agent."""
from __future__ import annotations

import json

import pytest

from app.evaluation import EvalRun, build_context, summarize
from app.fixtures import load_eval_cases


@pytest.mark.asyncio
async def test_all_labeled_cases_match_rules_only_workflow(tmp_path):
    cases = load_eval_cases()
    assert len(cases) >= 30
    results = []
    for case in cases:
        ctx = build_context(str(tmp_path / case["id"]))
        (tmp_path / case["id"]).mkdir(exist_ok=True)
        ctx = build_context(str(tmp_path / case["id"]))
        results.append(await EvalRun(ctx, via_agent=False).run(case))
    failures = [(r["id"], r["mismatches"]) for r in results if not r["passed"]]
    assert not failures, json.dumps(failures, indent=2, default=str)
    summary = summarize(results)
    assert summary["passed"] == summary["total"]
    assert summary["unsupported_claims_total"] == 0
    assert summary["duplicate_submits"] == 0
    assert summary["held_out_total"] >= 8


@pytest.mark.asyncio
async def test_agent_path_matches_rules_only_on_completion_cases(tmp_path):
    cases = [c for c in load_eval_cases() if c["category"] in ("ordinary_completion", "refusal") and c.get("scenario") is None and not c["needs_overrides"].get("state_code")]
    results = []
    for case in cases:
        (tmp_path / case["id"]).mkdir(exist_ok=True)
        ctx = build_context(str(tmp_path / case["id"]))
        results.append(await EvalRun(ctx, via_agent=True).run(case))
    failures = [(r["id"], r["mismatches"]) for r in results if not r["passed"]]
    assert not failures, json.dumps(failures, indent=2, default=str)
    assert all(r["tool_runs"] > 0 for r in results)
