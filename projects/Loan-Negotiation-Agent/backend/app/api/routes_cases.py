"""Customer-facing case endpoints."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..container import Container
from ..persistence.models import Action, Case, Customer, Document, Message, Mortgage, RefinanceApplication
from ..workflows import states as st
from ..workflows.approvals import approve_action
from ..workflows.states import WorkflowError, record_event, transition
from . import schemas
from .deps import current_customer, get_container, get_session

router = APIRouter(prefix="/v1", tags=["loan-cases"])


def _handle(exc: WorkflowError) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 400), detail=str(exc))


@router.get("/me")
def me(customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    mortgages = session.execute(select(Mortgage).where(Mortgage.customer_id == customer.id)).scalars().all()
    documents = session.execute(select(Document).where(Document.owner_customer_id == customer.id)).scalars().all()
    cases = session.execute(select(Case).where(Case.customer_id == customer.id).order_by(Case.created_at.desc())).scalars().all()
    return {
        "customer": {"id": customer.id, "display_name": customer.display_name, "tenant_id": customer.tenant_id, "verified_facts": {k: v for k, v in customer.verified_facts_json.items() if k != "ssn"}},
        "mortgages": [{"id": m.id, "balance_minor": m.balance_minor, "note_rate_decimal": m.note_rate_decimal, "remaining_months": m.remaining_months, "monthly_pi_minor": m.monthly_pi_minor, "escrow_minor": m.escrow_minor, "as_of": m.as_of.isoformat() if m.as_of else None} for m in mortgages],
        "documents": [{"id": d.id, "kind": d.kind, "source": d.source, "captured_at": d.captured_at.isoformat() if d.captured_at else None} for d in documents],
        "cases": [{"id": c.id, "status": c.state, "version": c.version, "updated_at": c.updated_at.isoformat()} for c in cases],
        "lenders": [{"id": l["id"], "name": l["name"], "environment": l["environment"], "is_current_servicer": l.get("is_current_servicer", False)} for l in container.lenders.values()],
        "environment": container.settings.provider_environment,
        "clock_now": container.clock.now().isoformat(),
    }


@router.post("/loan-cases", response_model=schemas.LoanCaseCreated, status_code=201)
def create_case(body: schemas.CreateLoanCaseRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)):
    if body.customer_id != customer.id:
        raise HTTPException(status_code=403, detail="customer_id does not match the authenticated customer")
    try:
        case = container.service.create_case(session, customer, body.mortgage_id, body.holding_horizon_months, body.maximum_cash_to_close_minor, body.offer_document_ids)
    except WorkflowError as exc:
        raise _handle(exc)
    return schemas.LoanCaseCreated(id=case.id, status=case.state, version=case.version, missing_fields=case.missing_fields_json, outstanding_questions=case.outstanding_questions_json)


@router.get("/loan-cases/{case_id}")
def get_case(case_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.service.case_view(session, case)
    except WorkflowError as exc:
        raise _handle(exc)


@router.get("/loan-cases/{case_id}/timeline")
def timeline(case_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return {"case_id": case.id, "status": case.state, "version": case.version, "events": container.service.timeline(session, case)}
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/facts")
def confirm_facts(case_id: str, body: schemas.ConfirmFactsRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        container.service.confirm_facts(session, case, body.model_dump(exclude_unset=True), f"customer:{customer.id}")
        return container.service.case_view(session, case)
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/offers", status_code=201)
def add_offers(case_id: str, body: schemas.AddOffersRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        offers = container.service.add_offer_documents(session, case, customer, body.document_ids, body.documents)
        return {"case_id": case.id, "status": case.state, "version": case.version, "offer_ids": [o.id for o in offers], "missing_fields": case.missing_fields_json, "outstanding_questions": case.outstanding_questions_json}
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/offers/{offer_id}/fields")
def supply_offer_field(case_id: str, offer_id: str, body: schemas.SupplyOfferFieldRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        offer = container.service.supply_offer_field(session, case, offer_id, body.field, body.value, body.source, f"customer:{customer.id}")
        return {"case_id": case.id, "offer_id": offer.id, "normalized": offer.normalized_json.get("normalized"), "version": case.version, "missing_fields": case.missing_fields_json}
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/compare")
def compare(case_id: str, body: schemas.CompareRequest = None, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    body = body or schemas.CompareRequest()
    try:
        case = container.service.get_case(session, case_id, customer)
        comparison = container.service.run_comparison(session, case, f"customer:{customer.id}", horizon_override=body.horizon_months)
        return {"case_id": case.id, "status": case.state, "version": case.version, "comparison_id": comparison.id, "comparison": comparison.result_json}
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/decisions")
def decide(case_id: str, body: schemas.DecisionRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        container.service.decide_keep(session, case, f"customer:{customer.id}", expected_version=body.expected_case_version)
        return container.service.case_view(session, case)
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/lender-request-drafts", status_code=201)
def lender_request_draft(case_id: str, body: schemas.LenderRequestDraftRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.service.draft_lender_request(session, case, body.lender_id, f"customer:{customer.id}", body.target_offer_id, body.competing_offer_id, body.disclosed_document_ids, body.request_type)
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/application-drafts", status_code=201)
def application_draft(case_id: str, body: schemas.ApplicationDraftRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.service.draft_application(session, case, body.offer_id, f"customer:{customer.id}", body.document_ids, body.income_assertion_minor)
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/applications/{application_id}/document-releases", status_code=201)
def document_release(case_id: str, application_id: str, body: schemas.DocumentReleaseRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.service.draft_document_release(session, case, application_id, body.document_ids, f"customer:{customer.id}")
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/loan-cases/{case_id}/applications/{application_id}/closing-requests", status_code=201)
def closing_request(case_id: str, application_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.service.draft_closing_request(session, case, application_id, f"customer:{customer.id}")
    except WorkflowError as exc:
        raise _handle(exc)


@router.get("/loan-cases/{case_id}/applications/{application_id}/final-review")
def final_review(case_id: str, application_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
    except WorkflowError as exc:
        raise _handle(exc)
    app = session.get(RefinanceApplication, application_id)
    if app is None or app.case_id != case.id:
        raise HTTPException(status_code=404, detail="application not found")
    from ..domain.final_terms import diff_terms

    if app.final_terms_json is None:
        return {"application_id": app.id, "status": app.status, "final_terms": None, "review": None}
    review = diff_terms(app.packet_json["offer_terms"], app.final_terms_json)
    return {"application_id": app.id, "status": app.status, "approved_offer_terms": app.packet_json["offer_terms"], "final_terms": app.final_terms_json, "review": review.as_dict(), "is_funded": False, "closing_evidence": app.closing_evidence_json}


# ---------------------------------------------------------------- approvals
@router.post("/actions/{action_id}/approve")
def approve(action_id: str, body: schemas.ApproveActionRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    action = session.get(Action, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    try:
        case = container.service.get_case(session, action.case_id, customer)
        now = container.clock.now()
        approval = approve_action(session, case, action, customer.id, body.expected_case_version, body.action_payload_hash, body.approval_challenge_id, now, container.settings.approval_ttl_seconds)
        if action.type == "send_negotiation" and case.state == st.AWAITING_APPROVAL:
            transition(session, case, st.NEGOTIATION_PENDING, f"customer:{customer.id}", now, data={"action_id": action.id})
            from ..persistence.models import LenderRequest

            lr = session.get(LenderRequest, action.payload_json["lender_request_id"])
            lr.status = "approved"
            session.add(lr)
        elif action.type == "submit_application":
            app = session.get(RefinanceApplication, action.payload_json["application_id"])
            app.status = "approved"
            session.add(app)
        job = container.service.enqueue(session, "execute_action", case.id, {"action_id": action.id}, dedupe_key=f"exec:{action.id}")
        return {"action_id": action.id, "status": action.status, "approval_id": approval.id, "job_id": job.id, "case_status": case.state, "case_version": case.version, "note": "A background executor performs the action; poll the case for the result."}
    except WorkflowError as exc:
        raise _handle(exc)


@router.post("/actions/{action_id}/decline")
def decline(action_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    action = session.get(Action, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    try:
        case = container.service.get_case(session, action.case_id, customer)
    except WorkflowError as exc:
        raise _handle(exc)
    if action.status != "proposed":
        raise HTTPException(status_code=409, detail=f"action is {action.status}")
    now = container.clock.now()
    action.status = "cancelled"
    action.updated_at = now
    session.add(action)
    record_event(session, case, "action.declined_by_customer", f"customer:{customer.id}", now, {"action_id": action.id})
    if case.state == st.AWAITING_APPROVAL and st.can_transition(case.state, st.AWAITING_DECISION):
        transition(session, case, st.AWAITING_DECISION, f"customer:{customer.id}", now, data={"action_id": action.id, "reason": "declined by customer"})
    return {"action_id": action.id, "status": action.status, "case_status": case.state, "case_version": case.version}


# --------------------------------------------------------------------- chat
@router.get("/loan-cases/{case_id}/messages")
def list_messages(case_id: str, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
    except WorkflowError as exc:
        raise _handle(exc)
    msgs = session.execute(select(Message).where(Message.case_id == case.id).order_by(Message.created_at)).scalars().all()
    return {"case_id": case.id, "messages": [{"id": m.id, "role": m.role, "content": m.content, "data": m.data_json, "created_at": m.created_at.isoformat()} for m in msgs]}


@router.post("/loan-cases/{case_id}/messages")
def send_message(case_id: str, body: schemas.ChatRequest, customer: Customer = Depends(current_customer), session: Session = Depends(get_session), container: Container = Depends(get_container)) -> Dict[str, Any]:
    try:
        case = container.service.get_case(session, case_id, customer)
        return container.agent.handle_turn(session, case, customer, body.message, body.intent, body.params)
    except WorkflowError as exc:
        raise _handle(exc)
