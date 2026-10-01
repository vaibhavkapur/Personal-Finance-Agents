import hashlib
import json

def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def reconcile(forms, profile, complete):
    issues, exclusions, facts, seen, canonical, duplicates = [], [], [], {}, {}, []
    for form in forms:
        if form["tax_year"] != 2025:
            issues.append(f"{form['issuer_name']}: the tax year does not match 2025.")
            continue
        if form["taxpayer_ref"] != "SYNTHETIC-ALEX-2025":
            issues.append(f"{form['issuer_name']}: confirm the taxpayer identity.")
            continue
        if not form["confirmed"]:
            issues.append(f"Confirm the extracted fields for {form['issuer_name']}.")
        if form["unsupported_flags"]:
            exclusions.append(f"{form['issuer_name']}: unsupported fields: {', '.join(form['unsupported_flags'])}.")
        identity = (form["issuer_ref"], "1099-INT" if form["form_type"] == "1099-INT" else "W-2")
        content = {k: v for k, v in form.items() if k not in {"id", "captured_at", "content_hash", "confirmed", "confirmation_evidence", "original_extraction"}}
        fingerprint = digest(content)
        if fingerprint in seen:
            duplicates.append({"form_id": form["id"], "original_id": seen[fingerprint]})
            continue
        seen[fingerprint] = form["id"]
        if form["form_type"] == "W-2c":
            prior = canonical.get(identity)
            if not prior or prior["id"] != form["supersedes_form_id"]:
                issues.append("A correction must supersede the current W-2 from the same issuer.")
                continue
            updated = dict(prior)
            updated["id"] = form["id"]
            updated["lineage"] = dict(prior["lineage"])
            for field in ("wages_minor", "federal_withholding_minor"):
                if form[field] is not None:
                    updated[field] = form[field]
                    updated["lineage"][field] = form["id"]
            canonical[identity] = updated
        elif identity in canonical:
            issues.append(f"Conflicting forms from {form['issuer_name']}; supply an explicit correction.")
        else:
            canonical[identity] = {**form, "lineage": {key: form["id"] for key in ("wages_minor", "interest_minor", "federal_withholding_minor")}}
    for key, kind in (("expected_w2_issuers", "W-2"), ("expected_interest_issuers", "1099-INT")):
        for issuer in profile.get(key, []):
            if (issuer, kind) not in canonical:
                issues.append(f"Missing {kind} from {issuer.replace('fixture_', '').replace('_', ' ')}.")
    if not any(key[1] == "W-2" for key in canonical):
        issues.append("At least one confirmed W-2 is required.")
    if not complete:
        issues.append("Confirm that all wages, bank interest and withholding have been provided, including income with no form.")
    for record in canonical.values():
        is_interest = record["form_type"] == "1099-INT"
        fields = [("interest_minor", "2b", "Box 1"), ("federal_withholding_minor", "25b", "Box 4")] if is_interest else [("wages_minor", "1a", "Box 1"), ("federal_withholding_minor", "25a", "Box 2")]
        for field, line, locator in fields:
            facts.append({"line": line, "amount_minor": record[field], "currency": "USD", "source_form_id": record["lineage"][field], "source_locator": locator, "issuer_name": record["issuer_name"]})
    interest = sum(f["amount_minor"] for f in facts if f["line"] == "2b")
    wages = sum(f["amount_minor"] for f in facts if f["line"] == "1a")
    if interest > 150_000:
        exclusions.append("Bank interest exceeds $1,500; Schedule B is outside this prototype.")
    if wages + interest < 1_910_400 and profile.get("age_at_year_end", 0) >= 25:
        exclusions.append("Income may qualify for the earned income credit; specialist review is required.")
    if (wages + 50) // 100 + (interest + 50) // 100 - 15750 >= 100000:
        exclusions.append("Taxable income of $100,000 or more requires a separate computation pack.")
    return {"facts": facts, "facts_hash": digest(facts), "issues": list(dict.fromkeys(issues)), "exclusions": exclusions, "duplicates": duplicates, "complete": complete, "ready": not issues and not exclusions}
