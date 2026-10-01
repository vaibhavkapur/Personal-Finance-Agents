"""The evaluation harness itself: a labeled case runs end to end and the
claim checker flags unsupported statements."""

from __future__ import annotations

import json
import re

from app.agent.evaluation import CLAIM_RULES, _load_cases, run_case
from app.agent.llm import RulesPolicy
from app.config import FIXTURES_DIR


def test_eval_fixture_has_thirty_labeled_cases_with_heldout_split():
    data = _load_cases()
    cases = data["cases"]
    assert len(cases) >= 30
    assert {c["category"] for c in cases} == {"ordinary_completion", "missing_information", "conflicting_evidence", "refusal", "uncertain_provider"}
    assert sum(1 for c in cases if c["split"] == "heldout") >= 8
    assert len({c["id"] for c in cases}) == len(cases)


def test_single_case_runs_with_rules_policy():
    data = _load_cases()
    case = next(c for c in data["cases"] if c["id"] == "eval_019")
    result = run_case(case, data["story"], RulesPolicy)
    assert result.completed, result.failures
    assert result.final_state == "awaiting_approval"
    assert result.unsupported_claims == 0


def test_claim_checker_flags_unsupported_completion():
    hits = [pattern for pattern, ok in CLAIM_RULES if pattern.search("Done and verified, the money has moved.") and "evaluating" not in ok]
    assert len(hits) >= 1
    assert not any(p.search("No funds moved.") for p, _ in CLAIM_RULES[2:3])


def test_published_results_match_fixture_version():
    results = json.loads((FIXTURES_DIR / "eval_results.json").read_text())
    assert results["fixture_version"] == _load_cases()["fixture_version"]
    assert results["summary"]["all"]["cases"] >= 30
