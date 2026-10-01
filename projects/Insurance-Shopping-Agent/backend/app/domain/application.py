"""Build a reviewable application from confirmed answers and the selected quote.

The builder never submits anything and never fills an answer the customer did not give.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .hashing import sha256_hash
from .needs import UNKNOWN, InsuranceNeeds
from .quotes import RentersQuote

APPLICATION_SCHEMA = "renters-application/v1"


class IncompleteApplication(Exception):
    def __init__(self, missing_questions: List[Dict[str, Any]], message: Optional[str] = None) -> None:
        self.missing_questions = missing_questions
        super().__init__(message or "application is missing confirmed answers: %s" % [q["question_id"] for q in missing_questions])


class StaleQuote(Exception):
    pass


def answers_hash(answers: List[Dict[str, Any]]) -> str:
    """Hash only the (question_id, value) pairs so re-confirmation with the same values is stable."""
    return sha256_hash(sorted([[a["question_id"], a["value"]] for a in answers], key=lambda x: x[0]))


def build_application(
    case_id: str,
    customer_id: str,
    applicant: Dict[str, Any],
    needs: InsuranceNeeds,
    quote: RentersQuote,
    insurer_questions: List[Dict[str, Any]],
    confirmed_answers: Dict[str, Dict[str, Any]],
    now: datetime,
    revision: int = 1,
    disclosed_document_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the exact application payload for review.

    confirmed_answers maps question_id -> {"value", "confirmed_at", "answer_version", "evidence_id"}.
    Only questions this insurer asked are included; unknown answers block required questions.
    """
    if quote.status != "quoted":
        raise StaleQuote("quote %s is not in quoted status (%s)" % (quote.quote_ref, quote.status))
    if quote.valid_until < now:
        raise StaleQuote("quote %s expired at %s" % (quote.quote_ref, quote.valid_until.isoformat()))
    if quote.needs_version != needs.version:
        raise StaleQuote(
            "quote %s was issued for needs version %d but the current needs version is %d"
            % (quote.quote_ref, quote.needs_version, needs.version)
        )

    answers: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for question in insurer_questions:
        record = confirmed_answers.get(question["id"])
        if record is None or record.get("value") in (None, UNKNOWN):
            if question.get("required", True):
                missing.append({"question_id": question["id"], "text": question["text"], "insurer_id": quote.insurer_id})
            continue
        answers.append(
            {
                "question_id": question["id"],
                "question_text": question["text"],
                "value": record["value"],
                "confirmed_at": record.get("confirmed_at"),
                "answer_version": record.get("answer_version", 1),
                "evidence_id": record.get("evidence_id"),
            }
        )
    if missing:
        raise IncompleteApplication(missing)

    payload = {
        "schema": APPLICATION_SCHEMA,
        "case_id": case_id,
        "customer_id": customer_id,
        "insurer_id": quote.insurer_id,
        "insurer_name": quote.insurer_name,
        "quote_ref": quote.quote_ref,
        "quote_version": quote.quote_version,
        "needs_version": needs.version,
        "revision": revision,
        "applicant": {
            "display_name": applicant.get("display_name"),
            "address": needs.address.model_dump() if needs.address else UNKNOWN,
        },
        "terms": quote.material_terms(),
        "answers": answers,
        "answers_hash": answers_hash(answers),
        "disclosed_document_ids": list(disclosed_document_ids or []),
        "prepared_at": now.isoformat(),
        "irreversible_effects": [
            "Submitting sends the answers above to %s for underwriting." % quote.insurer_name,
            "If the insurer binds the policy, a premium of %s per year becomes payable at issuance (simulated in this prototype)."
            % _fmt(quote.annual_premium_minor, quote.currency),
        ],
    }
    return payload


def _fmt(amount_minor: int, currency: str) -> str:
    from .money import fmt_minor

    return fmt_minor(amount_minor, currency)


def application_payload_hash(payload: Dict[str, Any]) -> str:
    """Hash of everything an approval binds to (terms, answers, applicant, revision)."""
    binding = {
        "schema": payload["schema"],
        "case_id": payload["case_id"],
        "insurer_id": payload["insurer_id"],
        "quote_ref": payload["quote_ref"],
        "quote_version": payload["quote_version"],
        "needs_version": payload["needs_version"],
        "revision": payload["revision"],
        "applicant": payload["applicant"],
        "terms": payload["terms"],
        "answers_hash": payload["answers_hash"],
        "disclosed_document_ids": payload["disclosed_document_ids"],
    }
    return sha256_hash(binding)
