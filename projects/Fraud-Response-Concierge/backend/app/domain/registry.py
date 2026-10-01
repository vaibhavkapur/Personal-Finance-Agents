"""Versioned, trusted synthetic records. No URLs supplied by customers are used."""
from copy import deepcopy

CUSTOMER = "cus_demo_9"
PROVIDERS = {
    "cedar": {"id": "cedar", "name": "Cedar Credit", "short": "C", "kind": "credit", "channel": "Cedar simulated secure center", "handoff_path": "/handoff/cedar", "profile_version": "fixture-2026-09-v1", "deadline_days": 14, "deadline_label": "Credit report follow-up", "source": "Synthetic Cedar reporting profile v1; not a statutory deadline", "guidance": "https://www.consumerfinance.gov/ask-cfpb/am-i-responsible-for-unauthorized-charges-if-my-credit-cards-are-lost-or-stolen-en-29/"},
    "northstar": {"id": "northstar", "name": "Northstar Bank", "short": "N", "kind": "debit", "channel": "Northstar simulated secure center", "handoff_path": "/handoff/northstar", "profile_version": "fixture-2026-09-v1", "deadline_days": 2, "deadline_label": "Debit report follow-up", "source": "Synthetic Northstar reporting profile v1; not a statutory deadline", "guidance": "https://www.consumerfinance.gov/ask-cfpb/how-do-i-get-my-money-back-after-i-discover-an-unauthorized-transaction-or-money-missing-from-my-bank-account-en-1017/"},
}
INSTRUMENTS = [
    {"id": "credit_card_demo", "provider_id": "cedar", "name": "Everyday credit", "masked_identifier": "•••• 4829", "instrument_type": "credit", "capabilities": {"lock": True, "report_lost": True, "replacement": True, "direct_auth": False}, "protection_status": "unprotected", "replacement_status": "not_requested", "lost_report_status": "not_reported"},
    {"id": "debit_card_demo", "provider_id": "northstar", "name": "Everyday debit", "masked_identifier": "•••• 9016", "instrument_type": "debit", "capabilities": {"lock": True, "report_lost": True, "replacement": True, "direct_auth": True}, "protection_status": "unprotected", "replacement_status": "not_requested", "lost_report_status": "not_reported"},
]
TRANSACTIONS = [
    {"id": "txn_demo_91", "instrument_id": "credit_card_demo", "provider_id": "cedar", "merchant": "NOVA ELECTRONICS", "amount_minor": 24999, "currency": "USD", "occurred_at": "2026-09-25T12:48:00Z", "statement_date": "2026-09-24", "context": "Online purchase. Merchant: Nova Electronics, Portland, OR. No receipt matched in the synthetic records.", "evidence_status": "unrecognized", "customer_statement": None, "provider_determination": None, "investigation_status": "not_reported", "credit": {"provisional_minor": 0, "final_minor": 0, "reversed_minor": 0}},
    {"id": "txn_demo_92", "instrument_id": "debit_card_demo", "provider_id": "northstar", "merchant": "SQ * WESTSIDE COFFEE", "amount_minor": 1850, "currency": "USD", "occurred_at": "2026-09-25T09:15:00Z", "statement_date": "2026-09-24", "context": "Square descriptor for Westside Coffee, Brooklyn, NY. In-person purchase. This context does not establish whether you authorized it.", "evidence_status": "unrecognized", "customer_statement": None, "provider_determination": None, "investigation_status": "not_reported", "credit": {"provisional_minor": 0, "final_minor": 0, "reversed_minor": 0}},
]

def fixtures(instruments=None, transactions=None):
    return {"instruments": deepcopy([i for i in INSTRUMENTS if instruments is None or i["id"] in instruments]), "transactions": deepcopy([t for t in TRANSACTIONS if transactions is None or t["id"] in transactions]), "tasks": [], "identity_verified": False, "scenario": "normal", "narrative": "I lost my wallet and noticed purchases I do not recognize.", "narrative_confirmed_at": None}
