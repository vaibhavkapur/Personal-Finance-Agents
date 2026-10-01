from app.api.schemas import Form
from app.domain.scope import QUESTIONS

def profile():
    return {"tax_year": 2025, "jurisdiction": "US_FEDERAL", "filing_status": "single", "age_at_year_end": 32, "answers": {key: True for key in QUESTIONS}, "expected_w2_issuers": ["fixture_northstar", "fixture_atlas"], "expected_interest_issuers": ["fixture_harbor"]}

def forms(scenario="two_jobs"):
    records = [
        {"form_type": "W-2", "issuer_ref": "fixture_northstar", "issuer_name": "Northstar Studio", "wages_minor": 4_200_025, "federal_withholding_minor": 480_050, "social_security_minor": 260402, "medicare_minor": 60900},
        {"form_type": "W-2", "issuer_ref": "fixture_atlas", "issuer_name": "Atlas Technologies", "wages_minor": 3_000_025, "federal_withholding_minor": 340_000, "social_security_minor": 186002, "medicare_minor": 43500},
        {"form_type": "1099-INT", "issuer_ref": "fixture_harbor", "issuer_name": "Harbor Bank", "interest_minor": 24575, "federal_withholding_minor": 0},
    ]
    if scenario == "balance_due":
        records[0]["federal_withholding_minor"] = 300000
        records[1]["federal_withholding_minor"] = 200000
    return [Form(**record, confirmed=True).model_dump() for record in records]
