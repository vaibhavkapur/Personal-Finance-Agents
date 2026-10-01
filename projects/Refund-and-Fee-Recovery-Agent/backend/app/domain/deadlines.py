"""Versioned issuer / case-type deadline configuration.

The values shipped in fixtures/issuer_config.json are fixtures, not legal or
issuer rules. Verify applicable deadlines from current sources for a real case.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from ..clock import format_ts, parse_ts


@dataclass
class DeadlineConfig:
    version: str
    issuers: Dict[str, dict]

    @classmethod
    def load(cls, path: Path) -> "DeadlineConfig":
        raw = json.loads(path.read_text())
        return cls(version=raw["version"], issuers=raw["issuers"])


@dataclass
class Deadline:
    deadline_at: str
    source: str
    alert_days_before: int

    def alert(self, now: datetime) -> Optional[str]:
        remaining = parse_ts(self.deadline_at) - now
        if remaining.days < 0:
            return "deadline_passed"
        if remaining.days <= self.alert_days_before:
            return f"deadline_within_{remaining.days}_days"
        return None


def compute_dispute_deadline(config: DeadlineConfig, issuer_id: str, purchase_posted_at: str, case_type: str) -> Optional[Deadline]:
    issuer = config.issuers.get(issuer_id)
    if not issuer:
        return None
    rule = issuer.get("case_types", {}).get(case_type)
    if not rule:
        return None
    deadline = parse_ts(purchase_posted_at) + timedelta(days=int(rule["window_days"]))
    return Deadline(
        deadline_at=format_ts(deadline),
        source=f"{config.version}:{issuer_id}:{case_type}:{rule['source']}",
        alert_days_before=int(rule.get("alert_days_before", 14)),
    )
