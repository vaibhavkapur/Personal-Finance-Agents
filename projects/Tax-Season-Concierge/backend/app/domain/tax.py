import hashlib
import json
from pathlib import Path
from app.domain.documents import digest

RULE_DIR = Path(__file__).resolve().parents[3] / "rules/2025"

def load_rules():
    manifest = json.loads((RULE_DIR / "manifest.json").read_text())
    for source in manifest["sources"]:
        if hashlib.sha256((RULE_DIR / source["file"]).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("Pinned rule source checksum mismatch; calculation is disabled")
    return manifest, json.loads((RULE_DIR / "single-tax-table.json").read_text())

def whole_dollars(minor):
    return ((minor + 50) // 100) * 100

def table_tax(taxable_minor, rows):
    income = taxable_minor // 100
    for row in rows:
        if row["lower"] <= income < row["upper"]:
            return row["single"] * 100, row
    raise ValueError("Taxable income is outside the pinned Tax Table")

def calculate(facts, rule_pack_id):
    pack, rows = load_rules()
    if pack["id"] != rule_pack_id:
        raise ValueError("Unknown or unreviewed rule pack")
    lines = []
    amounts = {}
    def line(number, label, amount, source_lines=None, rule=None):
        source_lines = source_lines or [number]
        imported = [f for f in facts if f["line"] in source_lines]
        source_ids = list(dict.fromkeys(f["source_form_id"] for f in imported))
        for parent in source_lines:
            prior = next((item for item in lines if item["line"] == parent), None)
            if prior:
                source_ids.extend(prior["source_form_ids"])
        value = {"line": number, "label": label, "amount_minor": amount, "currency": "USD", "source_form_ids": list(dict.fromkeys(source_ids)), "depends_on": source_lines, "rule_ref": rule or f"2025 Form 1040 instructions, line {number}", "rule_pack_id": pack["id"]}
        amounts[number] = amount
        lines.append(value)
    for number, label in [("1a", "Wages from W-2s"), ("2b", "Taxable bank interest")]:
        line(number, label, whole_dollars(sum(f["amount_minor"] for f in facts if f["line"] == number)))
    line("1z", "Total wages", amounts["1a"], ["1a"])
    line("9", "Total income", amounts["1z"] + amounts["2b"], ["1z", "2b"])
    line("10", "Adjustments to income", 0, [], "Eligibility questionnaire: no adjustments")
    line("11a", "Adjusted gross income", amounts["9"], ["9", "10"])
    line("11b", "Adjusted gross income carried forward", amounts["11a"], ["11a"])
    line("12e", "Standard deduction", pack["standard_deduction_minor"], [], "2025 Form 1040 instructions p. 35, single standard deduction")
    line("13a", "Qualified business income deduction", 0, [], "Eligibility questionnaire: no business income")
    line("13b", "Additional deductions", 0, [], "Eligibility questionnaire: no Schedule 1-A")
    line("14", "Total deductions", amounts["12e"], ["12e", "13a", "13b"])
    line("15", "Taxable income", max(0, amounts["11b"] - amounts["14"]), ["11b", "14"])
    tax, row = table_tax(amounts["15"], rows)
    line("16", "Federal income tax", tax, ["15"], f"2025 Tax Table: single, ${row['lower']:,} to less than ${row['upper']:,}")
    for n, label in [("17", "Additional taxes"), ("19", "Child and dependent credits"), ("20", "Other credits")]:
        line(n, label, 0, [], "Eligibility questionnaire: excluded schedules and credits")
    line("18", "Tax before credits", tax, ["16", "17"])
    line("21", "Total credits", 0, ["19", "20"])
    line("22", "Tax after credits", tax, ["18", "21"])
    line("23", "Other taxes", 0, [], "Eligibility questionnaire: no other taxes")
    line("24", "Total tax", tax, ["22", "23"])
    for number, label in [("25a", "Federal withholding from W-2s"), ("25b", "Federal withholding from 1099s")]:
        line(number, label, whole_dollars(sum(f["amount_minor"] for f in facts if f["line"] == number)))
    line("25c", "Other federal withholding", 0, [], "Eligibility questionnaire: no other withholding")
    withholding = amounts["25a"] + amounts["25b"]
    line("25d", "Total federal withholding", withholding, ["25a", "25b", "25c"])
    line("26", "Estimated payments", 0, [], "Eligibility questionnaire: no estimated payments")
    line("32", "Refundable credits and other payments", 0, [], "Eligibility questionnaire: no credits or other payments")
    line("33", "Total payments", withholding, ["25d", "26", "32"])
    refund, due = max(0, withholding-tax), max(0, tax-withholding)
    line("34", "Overpayment", refund, ["33", "24"])
    line("35a", "Potential refund", refund, ["34"])
    line("36", "Applied to next year", 0, [], "Prototype: no carryforward election")
    line("37", "Amount due", due, ["24", "33"])
    return {"facts_hash": digest(facts), "rule_pack_id": pack["id"], "rule_manifest_hash": digest(pack), "worksheet": lines, "tax_minor": tax, "withholding_minor": withholding, "refund_minor": refund, "amount_due_minor": due, "currency": "USD", "tax_table_row": row, "rounding": pack["rounding"], "notice": "Draft worksheet only. Line 38 penalties, any accrued interest and payment execution are outside this prototype."}
