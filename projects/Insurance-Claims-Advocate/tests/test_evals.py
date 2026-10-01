"""Release gates over the labeled evaluation set (state and evidence, not wording)."""
from __future__ import annotations

from conftest import ROOT  # noqa: F401  (sys.path setup)
from app.evals import load_cases, run_all


def test_eval_set_has_required_coverage():
    cases = load_cases()
    assert len(cases) >= 30
    categories = {c["category"] for c in cases}
    assert categories == {"ordinary_completion", "missing_information", "conflicting_evidence", "refusal", "uncertain_provider"}
    assert sum(1 for c in cases if c["held_out"]) >= 5
    assert all(c["fixture_version"] for c in cases)


def test_agent_and_rules_only_pass_all_labeled_cases():
    report = run_all()
    for mode, m in report["summary"]["modes"].items():
        failures = [r for r in report["results"] if r["mode"] == mode and not r["passed"]]
        assert not failures, f"{mode}: " + "; ".join(f"{r['id']}: {r['mismatches']}" for r in failures)
        assert m["unsupported_statements"] == 0  # every submitted statement has provenance
        assert m["missed_questions"] == 0
    # incomplete-evidence cases stay open rather than claiming completion
    for r in report["results"]:
        if r["category"] == "missing_information" and not r["observed"].get("error"):
            assert r["observed"]["final_status"] in ("closed", "collecting", "evidence_requested")
    # no unapproved external writes: every insurer claim corresponds to a stored submission
    for r in report["results"]:
        obs = r["observed"]
        if obs.get("insurer_claims") is not None:
            assert obs["insurer_claims"] <= 1
            assert (obs["submissions"] > 0) == (obs["insurer_claims"] > 0)
