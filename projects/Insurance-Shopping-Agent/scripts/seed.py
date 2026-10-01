"""Seed the configured database with the synthetic households' starter cases.

  python scripts/seed.py            # uses DATABASE_URL (default sqlite:///./data/app.db)
  python scripts/seed.py --reset    # drop and recreate tables first
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.context import AppContext  # noqa: E402
from app.fixtures import load_households, load_insurers, load_state_profile  # noqa: E402
from app.workflows.case_service import CaseService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    ctx = AppContext()
    if args.reset:
        ctx.db.drop_all()
        ctx.db.create_all()
    profile = load_state_profile()
    cases = CaseService(ctx)
    print("environment=%s adapter_mode=%s db=%s now=%s" % (ctx.environment, ctx.registry.mode, ctx.db.url, ctx.now().isoformat()))
    print("state profile: %s (%s), product %s, fixture %s" % (profile["state_name"], profile["state_code"], profile["product"], profile["fixture_version"]))
    for insurer in load_insurers():
        print("insurer %s: %s form %s" % (insurer["label"], insurer["display_name"], insurer["policy_form"]["policy_form_version"]))
    for hh in load_households()["households"]:
        created = cases.create_case(hh["customer_id"], {
            "state_code": "CA", "product": "renters", "desired_effective_date": "2026-11-01",
            "property_limit_minor": 3000000, "liability_limit_minor": 10000000, "replacement_cost_required": True,
        })
        print("seeded case %s for %s (%s) token=%s missing=%s" % (created["id"], hh["display_name"], hh["customer_id"], hh["auth_token"], created["missing_fields"]))
    print("operator token=%s" % load_households()["operator"]["auth_token"])


if __name__ == "__main__":
    main()
