QUESTIONS = {
    "us_resident": (True, "I was a US resident for all of 2025."),
    "not_dependent": (True, "No one can claim me as a dependent."),
    "not_blind": (True, "I am not legally blind."),
    "no_dependents": (True, "I have no dependents."),
    "ordinary_wages_only": (True, "My wages are ordinary W-2 wages, with no tips, qualified overtime, statutory employee or special wage treatment."),
    "no_other_income": (True, "I have no business, investment, retirement, unemployment, foreign or other income beyond these wages and ordinary bank interest."),
    "no_adjustments": (True, "I have no income adjustments, such as IRA, HSA or student loan deductions."),
    "no_credits": (True, "I have no tax credits to claim, including education, retirement savings or earned income credits."),
    "no_special_deductions": (True, "I have no tips, overtime, car loan interest or other special deductions."),
    "standard_deduction": (True, "I will use the standard deduction."),
    "no_foreign_accounts": (True, "I have no foreign accounts, trusts or foreign asset reporting requirements."),
    "ordinary_interest_only": (True, "Interest is from domestic bank deposits; no nominees, bonds, OID, adjustments or other Schedule B requirements."),
    "no_marketplace": (True, "I have no Marketplace coverage or advance premium tax credit to reconcile."),
    "no_other_taxes": (True, "I have no other federal taxes, household employment, excess retirement contributions or alternative minimum tax items."),
    "no_estimated_payments": (True, "I have no estimated payments, prior-year overpayments or extension payments."),
    "no_digital_assets": (True, "I had no reportable digital asset transactions."),
    "no_community_property": (True, "I have no community property allocation or registered domestic partner filing requirement."),
    "no_other_requirements": (True, "I have no other federal forms, schedules, special elections or filing requirements."),
    "not_already_filed": (True, "I have not already filed a 2025 federal return."),
}

def check_profile(profile):
    reasons, missing = [], []
    for key, wanted in [("tax_year", 2025), ("jurisdiction", "US_FEDERAL"), ("filing_status", "single")]:
        if profile.get(key) != wanted:
            reasons.append(f"Only {key}={wanted} is supported.")
    age = profile.get("age_at_year_end")
    if age is None:
        missing.append("age_at_year_end")
    elif not 18 <= age < 65:
        reasons.append("Only adults aged 18–64 at year end are supported.")
    answers = profile.get("answers", {})
    for key, (wanted, label) in QUESTIONS.items():
        if answers.get(key) is None:
            missing.append(key)
        elif answers[key] != wanted:
            reasons.append(label + " This requirement was not confirmed.")
    if set(answers) - QUESTIONS.keys():
        reasons.append("The questionnaire contains unknown scope answers.")
    return {"supported": not reasons and not missing, "reasons": reasons, "missing": missing}
