"""Policy interpreter: selects the policy version applicable to the loss date and exposes the reviewer-approved rule set.
Ambiguity is preserved: anything not covered by an approved rule is reported as `unknown`, never decided silently."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Clause:
    id: str
    heading: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Rule:
    rule_id: str
    clause_id: str
    value: Any


@dataclass(frozen=True)
class PolicyVersion:
    policy_id: str
    version: str
    effective_from: date
    effective_to: Optional[date]
    source_text: str
    clauses: Dict[str, Clause]
    rules: Dict[str, Rule]
    currency: str
    approved_by: str
    approved_at: str
    insurer_name: str

    def rule(self, name: str) -> Rule:
        if name not in self.rules:
            raise PolicyError(f"no approved rule '{name}' in policy version {self.version}")
        return self.rules[name]

    def clause_for_rule(self, name: str) -> Clause:
        return self.clauses[self.rule(name).clause_id]

    def citation(self, name: str) -> Dict[str, Any]:
        rule = self.rule(name)
        clause = self.clauses[rule.clause_id]
        return {
            "rule_id": rule.rule_id,
            "clause_id": clause.id,
            "heading": clause.heading,
            "policy_version": self.version,
            "text": clause.text,
            "source_offsets": [clause.start, clause.end],
        }

    def to_public(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "insurer_name": self.insurer_name,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "currency": self.currency,
            "rules": {k: {"rule_id": r.rule_id, "clause_id": r.clause_id, "value": r.value} for k, r in self.rules.items()},
            "clauses": [
                {"id": c.id, "heading": c.heading, "text": c.text, "source_offsets": [c.start, c.end]} for c in self.clauses.values()
            ],
            "rule_set_approval": {"approved_by": self.approved_by, "approved_at": self.approved_at},
        }


def _parse_version(policy_id: str, insurer_name: str, raw: Dict[str, Any]) -> PolicyVersion:
    text = raw["source_text"]
    clauses = {}
    for c in raw["clauses"]:
        clauses[c["id"]] = Clause(id=c["id"], heading=c["heading"], text=text[c["start"]:c["end"]].strip(), start=c["start"], end=c["end"])
    rules: Dict[str, Rule] = {}
    raw_rules = raw["rules"]
    for name, spec in raw_rules.items():
        if isinstance(spec, dict) and "rule_id" in spec:
            if spec["clause_id"] not in clauses:
                raise PolicyError(f"rule {spec['rule_id']} references unknown clause {spec['clause_id']}")
            rules[name] = Rule(rule_id=spec["rule_id"], clause_id=spec["clause_id"], value=spec["value"])
    return PolicyVersion(
        policy_id=policy_id,
        version=raw["version"],
        effective_from=date.fromisoformat(raw["effective_from"]),
        effective_to=date.fromisoformat(raw["effective_to"]) if raw.get("effective_to") else None,
        source_text=text,
        clauses=clauses,
        rules=rules,
        currency=raw_rules.get("currency", "USD"),
        approved_by=raw_rules.get("approved_by", ""),
        approved_at=raw_rules.get("approved_at", ""),
        insurer_name=insurer_name,
    )


class PolicyFixture:
    def __init__(self, fixture: Dict[str, Any]):
        self.policy_id: str = fixture["policy_id"]
        self.insurer_name: str = fixture.get("insurer_name", "")
        self.customer_id: str = fixture["policyholder_customer_id"]
        self.versions: List[PolicyVersion] = [_parse_version(self.policy_id, self.insurer_name, v) for v in fixture["versions"]]

    def version_for_loss(self, loss_at: datetime) -> PolicyVersion:
        loss_day = loss_at.date()
        matches = [v for v in self.versions if v.effective_from <= loss_day and (v.effective_to is None or loss_day <= v.effective_to)]
        if not matches:
            raise PolicyError(f"no policy version of {self.policy_id} is effective on {loss_day.isoformat()}")
        if len(matches) > 1:
            raise PolicyError(f"overlapping policy versions for {loss_day.isoformat()}: {[m.version for m in matches]}")
        return matches[0]

    def get_version(self, version: str) -> PolicyVersion:
        for v in self.versions:
            if v.version == version:
                return v
        raise PolicyError(f"unknown policy version {version}")

    def latest(self) -> PolicyVersion:
        return sorted(self.versions, key=lambda v: v.effective_from)[-1]
