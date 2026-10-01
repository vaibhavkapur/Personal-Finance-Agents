"""Case lifecycle: interview, quote coordination, comparison and case views.

Provider calls happen outside database transactions: a pending record is persisted
first, the provider is called, then the result is reconciled in a new transaction.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..adapters.base import ProviderError, ProviderMalformedResponse, ProviderTimeout
from ..context import AppContext
from ..domain.coverage import compare_coverage
from ..domain.money import fmt_minor
from ..domain.needs import UNKNOWN, InsuranceNeeds, apply_needs_update
from ..domain.quotes import RentersQuote
from ..fixtures import household_by_customer, load_state_profile
from ..persistence import models as m
from ..persistence import repositories as repo
from ..persistence.db import new_id
from ..persistence.jobs import enqueue_job
from . import states as st
from .states import record_event, transition

ACTIVE_TASK_STATUSES = ("pending", "input_required", "quoted", "timeout", "failed")


class CaseError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class CaseService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.registry = ctx.registry

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.ctx.now()

    def _env(self) -> str:
        return self.ctx.environment

    def _load(self, session: Session, case_id: str, customer_id: Optional[str]) -> Tuple[m.Case, InsuranceNeeds]:
        case = repo.get_case_for_customer(session, case_id, customer_id) if customer_id else repo.get_case(session, case_id)
        return case, repo.current_needs(session, case)

    # ------------------------------------------------------------------ create
    def create_case(self, customer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile = load_state_profile()
        state_code = str(payload.get("state_code", "")).upper()
        if state_code != profile["state_code"]:
            raise CaseError("only state %s is supported in this release" % profile["state_code"], 422, {"supported_states": [profile["state_code"]]})
        try:
            household = household_by_customer(customer_id)
        except KeyError:
            raise CaseError("unknown customer", 404)
        needs_data = {
            "customer_id": customer_id,
            "state_code": state_code,
            "product": payload.get("product", "renters"),
            "version": 1,
            "effective_date": payload.get("desired_effective_date"),
            "property_limit_minor": payload.get("property_limit_minor"),
            "liability_limit_minor": payload.get("liability_limit_minor"),
            "deductible_cap_minor": payload.get("deductible_cap_minor"),
            "replacement_cost_required": payload.get("replacement_cost_required"),
            "required_item_classes": payload.get("required_item_classes"),
            "address": payload.get("address"),
            "deductible_preference": payload.get("deductible_preference"),
        }
        try:
            needs = InsuranceNeeds(**needs_data)
        except ValidationError as exc:
            errors = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg"))} for e in exc.errors()]
            raise CaseError("invalid needs: %s" % errors[0]["msg"], 422, {"errors": errors})
        self._validate_effective_date(needs, profile)
        now = self.now()
        with self.db.session() as session:
            case = m.Case(
                id=new_id("shopcase"),
                tenant_id=customer_id,
                customer_id=customer_id,
                workflow_type="insurance_shopping",
                state=st.COLLECTING,
                version=0,
                created_at=now,
                updated_at=now,
            )
            session.add(case)
            session.flush()
            repo.save_needs_version(session, case, needs, now)
            record_event(session, case, "insurance.case.created", "customer:" + customer_id, now, self._env(),
                         {"customer_display_name": household["display_name"], "needs_version": 1}, to_state=st.COLLECTING)
            session.flush()
            return {"id": case.id, "status": case.state, "version": case.version, "missing_fields": needs.missing_fields(), "needs_version": needs.version}

    def _validate_effective_date(self, needs: InsuranceNeeds, profile: Dict[str, Any]) -> None:
        if needs.effective_date is None:
            return
        today = self.now().date()
        lead = (needs.effective_date - today).days
        if lead < profile["min_effective_date_lead_days"] or lead > profile["max_effective_date_lead_days"]:
            raise CaseError(
                "desired_effective_date must be between %d and %d days after %s"
                % (profile["min_effective_date_lead_days"], profile["max_effective_date_lead_days"], today.isoformat()),
                422,
            )

    # ------------------------------------------------------------------ answers
    async def record_answers(self, case_id: str, customer_id: Optional[str], answers: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        now = self.now()
        requote_insurers: List[str] = []
        pending_provider_answers: List[Tuple[str, str, Dict[str, Any]]] = []  # (task_id, insurer_id, answer)
        recorded: List[Dict[str, Any]] = []
        with self.db.session() as session:
            case, needs = self._load(session, case_id, customer_id)
            if case.state in st.TERMINAL:
                raise CaseError("case is completed", 409)
            post_submission = case.state not in st.PRE_SUBMISSION and case.state != st.MANUAL_REVIEW
            needs_updates: Dict[str, Any] = {}
            question_answers: List[Dict[str, Any]] = []
            for item in answers:
                field = item.get("field")
                qid = item.get("question_id")
                if qid and qid.startswith("needs:"):
                    field, qid = qid.split(":", 1)[1], None
                if field:
                    needs_updates[field] = item.get("value")
                elif qid:
                    question_answers.append({"question_id": qid, "value": item.get("value"), "evidence_id": item.get("evidence_id")})
                else:
                    raise CaseError("each answer needs a field or question_id", 422)

            if needs_updates:
                if post_submission:
                    raise CaseError("material inputs are locked after submission; open a new case", 409)
                try:
                    new_needs = apply_needs_update(needs, needs_updates)
                except (KeyError, ValidationError, ValueError) as exc:
                    raise CaseError("invalid needs update: %s" % exc, 422)
                self._validate_effective_date(new_needs, load_state_profile())
                if new_needs.version != needs.version:
                    repo.save_needs_version(session, case, new_needs, now)
                    had_tasks = bool(repo.quote_tasks_for_case(session, case.id, needs.version))
                    self._supersede_quotes(session, case, needs.version, "material input changed", now)
                    record_event(session, case, "insurance.needs.updated", actor, now, self._env(),
                                 {"needs_version": new_needs.version, "changed_fields": sorted(needs_updates.keys()), "requote_required": had_tasks})
                    if had_tasks:
                        requote_insurers = self.registry.ids()
                else:
                    row = repo.current_needs_row(session, case)
                    row.data_json = new_needs.model_dump(mode="json")
                    record_event(session, case, "insurance.needs.preference_updated", actor, now, self._env(), {"changed_fields": sorted(needs_updates.keys())})
                needs = new_needs
                recorded.append({"kind": "needs", "fields": sorted(needs_updates.keys()), "needs_version": needs.version})

            latest = repo.latest_answers(session, case.id)
            tasks = {t.insurer_id: t for t in repo.quote_tasks_for_case(session, case.id, needs.version) if t.status in ACTIVE_TASK_STATUSES}
            needs_row = repo.current_needs_row(session, case)
            for qa in question_answers:
                owner = self.registry.find_question_owner(qa["question_id"])
                if owner is None:
                    raise CaseError("unknown question_id %s" % qa["question_id"], 422)
                question = self.registry.question(owner, qa["question_id"])
                value = self._validate_answer(question, qa["value"])
                previous = latest.get(qa["question_id"])
                version = (previous.answer_version + 1) if previous else 1
                if previous and previous.answer_json.get("value") == value:
                    recorded.append({"kind": "answer", "question_id": qa["question_id"], "unchanged": True})
                    continue
                if post_submission and question.get("material", True):
                    raise CaseError("underwriting answers are locked after submission", 409)
                row = m.UnderwritingAnswer(
                    id=new_id("ans"),
                    case_id=case.id,
                    needs_id=needs_row.id,
                    question_id=qa["question_id"],
                    provider_id=owner,
                    answer_json={"value": value, "question_text": question["text"]},
                    confirmed_at=now,
                    evidence_id=qa.get("evidence_id") or "customer_statement",
                    answer_version=version,
                )
                session.add(row)
                latest[qa["question_id"]] = row
                recorded.append({"kind": "answer", "question_id": qa["question_id"], "insurer_id": owner, "answer_version": version, "value": value})
                record_event(session, case, "insurance.answer.confirmed", actor, now, self._env(),
                             {"question_id": qa["question_id"], "insurer_id": owner, "answer_version": version, "unknown": value == UNKNOWN})
                task = tasks.get(owner)
                if task is not None:
                    blocking = any(q.get("id") == qa["question_id"] for q in (task.open_questions_json or []))
                    if task.status == "input_required" and blocking and task.external_task_id:
                        pending_provider_answers.append((task.id, owner, {"question_id": qa["question_id"], "value": value}))
                    elif task.status == "quoted" and previous is not None and question.get("material", True) and owner not in requote_insurers:
                        # A material answer changed after quoting: the existing quote is stale.
                        requote_insurers.append(owner)

            if requote_insurers:
                self._invalidate_open_actions(session, case, "material input changed", now)
                for insurer_id in requote_insurers:
                    for task in repo.quote_tasks_for_case(session, case.id, needs.version):
                        if task.insurer_id == insurer_id and task.status in ACTIVE_TASK_STATUSES:
                            task.status = "superseded"
                            task.updated_at = now
                    for quote in repo.current_quotes(session, case.id, needs.version):
                        if quote.insurer_id == insurer_id:
                            quote.superseded_by = "requote:%s" % now.isoformat()
                if case.state in (st.AWAITING_APPROVAL, st.AWAITING_SELECTION, st.COMPARING, st.NEEDS_INFORMATION):
                    transition(session, case, st.QUOTING, actor, "insurance.quotes.requote_required", now, self._env(), {"insurers": requote_insurers})
            session.flush()
            case_id_local, needs_version = case.id, needs.version

        for task_id, insurer_id, answer in pending_provider_answers:
            await self._forward_answer(case_id_local, task_id, insurer_id, answer)

        if requote_insurers:
            await self.request_quotes(case_id_local, customer_id, actor, only_insurers=requote_insurers)
        else:
            with self.db.session() as session:
                case = repo.get_case(session, case_id_local)
                self._settle_state(session, case, actor, self.now())

        return {"recorded": recorded, "requoted_insurers": requote_insurers, **self.case_view(case_id_local, customer_id)}

    @staticmethod
    def _validate_answer(question: Dict[str, Any], value: Any) -> Any:
        if value in (None, UNKNOWN, "unknown"):
            return UNKNOWN
        qtype = question.get("type", "boolean")
        if qtype == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("yes", "true", "no", "false"):
                return value.lower() in ("yes", "true")
            raise CaseError("question %s expects yes/no or unknown" % question["id"], 422)
        if qtype == "choice":
            if value not in question.get("choices", []):
                raise CaseError("question %s expects one of %s" % (question["id"], question.get("choices")), 422)
            return value
        return value

    async def _forward_answer(self, case_id: str, task_id: str, insurer_id: str, answer: Dict[str, Any]) -> None:
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            external = task.external_task_id if task else None
        if not external:
            return
        adapter = self.registry.get(insurer_id)
        try:
            result = await self.ctx.call_provider(case_id, insurer_id, "answer_question", adapter.answer_question(external, answer), request=answer)
        except ProviderTimeout as exc:
            self._mark_task_uncertain(task_id, str(exc))
            return
        except (ProviderError, KeyError, ValueError) as exc:
            self._mark_task_failed(task_id, str(exc))
            return
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            case = repo.get_case(session, task.case_id)
            self._apply_task_result(session, case, task, result, self.now())

    # ------------------------------------------------------------------ quotes
    async def request_quotes(self, case_id: str, customer_id: Optional[str], actor: str, only_insurers: Optional[List[str]] = None) -> Dict[str, Any]:
        now = self.now()
        dispatch: List[Tuple[str, str, str, Dict[str, Any], Dict[str, Any]]] = []
        with self.db.session() as session:
            case, needs = self._load(session, case_id, customer_id)
            if case.state not in (st.COLLECTING, st.QUOTING, st.NEEDS_INFORMATION, st.COMPARING, st.AWAITING_SELECTION, st.EXPIRED, st.DECLINED):
                raise CaseError("quotes cannot be requested while the case is %s" % case.state, 409)
            missing = [f for f in needs.missing_fields() if f != "deductible_preference"]
            if not needs.is_ready_for_quoting():
                raise CaseError("needs are incomplete", 409, {"missing_fields": missing})
            if case.state == st.EXPIRED:
                # Fresh quotes replace the expired set for the same needs version.
                self._supersede_quotes(session, case, needs.version, "quotes expired", now)
            existing = {t.insurer_id: t for t in repo.quote_tasks_for_case(session, case.id, needs.version) if t.status in ACTIVE_TASK_STATUSES}
            correlation_id = "corr_%s_v%d" % (case.id, needs.version)
            answers = repo.answers_as_records(session, case.id)
            needs_row = repo.current_needs_row(session, case)
            targets = only_insurers or self.registry.ids()
            all_tasks = repo.quote_tasks_for_case(session, case.id)
            for insurer_id in targets:
                if insurer_id in existing:
                    continue
                attempt = sum(1 for t in all_tasks if t.insurer_id == insurer_id) + 1
                request_ref = "%s:v%d:%s:quote:%d" % (case.id, needs.version, insurer_id, attempt)
                task = m.QuoteTask(
                    id=new_id("qtask"),
                    case_id=case.id,
                    needs_id=needs_row.id,
                    needs_version=needs.version,
                    insurer_id=insurer_id,
                    correlation_id=correlation_id,
                    request_ref=request_ref,
                    status="pending",
                    open_questions_json=[],
                    environment=self._env(),
                    created_at=now,
                    updated_at=now,
                )
                session.add(task)
                session.flush()
                # Data minimisation: send only this insurer's own question answers.
                insurer_answers = {
                    q["id"]: answers[q["id"]]["value"]
                    for q in self.registry.questions(insurer_id)
                    if q["id"] in answers and answers[q["id"]]["value"] != UNKNOWN
                }
                requirements = needs.to_requirements()
                requirements["correlation_id"] = correlation_id
                dispatch.append((task.id, insurer_id, request_ref, requirements, insurer_answers))
            if case.state in (st.COLLECTING, st.EXPIRED, st.DECLINED, st.AWAITING_SELECTION, st.COMPARING, st.NEEDS_INFORMATION):
                if case.state != st.QUOTING and dispatch:
                    transition(session, case, st.QUOTING, actor, "insurance.quotes.requested", now, self._env(),
                               {"needs_version": needs.version, "insurers": [d[1] for d in dispatch], "correlation_id": correlation_id})
            case_id_local = case.id

        for task_id, insurer_id, request_ref, requirements, insurer_answers in dispatch:
            await self._dispatch_quote_request(case_id_local, task_id, insurer_id, request_ref, requirements, insurer_answers)

        with self.db.session() as session:
            case = repo.get_case(session, case_id_local)
            self._settle_state(session, case, actor, self.now())
            needs = repo.current_needs(session, case)
            tasks = repo.quote_tasks_for_case(session, case.id, needs.version)
            return {
                "case_id": case.id,
                "status": case.state,
                "version": case.version,
                "needs_version": needs.version,
                "tasks": [self._task_summary(t) for t in tasks if t.status != "superseded"],
            }

    async def _dispatch_quote_request(self, case_id: str, task_id: str, insurer_id: str, request_ref: str,
                                      requirements: Dict[str, Any], insurer_answers: Dict[str, Any]) -> None:
        adapter = self.registry.get(insurer_id)
        try:
            result = await self.ctx.call_provider(
                case_id, insurer_id, "request_quote", adapter.request_quote(requirements, request_ref, insurer_answers),
                request_ref=request_ref, request={"needs_version": requirements.get("needs_version"), "answer_ids": sorted(insurer_answers)},
            )
        except ProviderTimeout as exc:
            self._mark_task_uncertain(task_id, str(exc))
            return
        except ProviderMalformedResponse as exc:
            self._mark_task_failed(task_id, "malformed response: %s" % exc, retry=True)
            return
        except (ProviderError, KeyError, ValueError) as exc:
            self._mark_task_failed(task_id, str(exc), retry=False)
            return
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            case = repo.get_case(session, case_id)
            self._apply_task_result(session, case, task, result, self.now())

    def _mark_task_uncertain(self, task_id: str, error: str) -> None:
        now = self.now()
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            if task is None or task.status == "superseded":
                return
            task.status = "timeout"
            task.last_error = error
            task.updated_at = now
            case = repo.get_case(session, task.case_id)
            record_event(session, case, "insurance.quote_task.timeout", "system", now, self._env(), {"task_id": task.id, "insurer_id": task.insurer_id, "outcome": "unknown"})
            enqueue_job(session, "poll_quote_task", {"task_id": task.id}, now + timedelta(seconds=2), dedupe_key="poll_quote_task:" + task.id)

    def _mark_task_failed(self, task_id: str, error: str, retry: bool = False) -> None:
        now = self.now()
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            if task is None or task.status == "superseded":
                return
            task.status = "failed"
            task.last_error = error
            task.updated_at = now
            case = repo.get_case(session, task.case_id)
            record_event(session, case, "insurance.quote_task.failed", "system", now, self._env(), {"task_id": task.id, "insurer_id": task.insurer_id, "error": error[:200], "retry": retry})
            if retry:
                enqueue_job(session, "poll_quote_task", {"task_id": task.id}, now + timedelta(seconds=2), dedupe_key="poll_quote_task:" + task.id)

    def _apply_task_result(self, session: Session, case: m.Case, task: m.QuoteTask, result: Dict[str, Any], now: datetime) -> None:
        if task.status == "superseded":
            return
        task.external_task_id = result.get("task_ref") or task.external_task_id
        task.updated_at = now
        status = result.get("status")
        if status == "quoted":
            try:
                quote = RentersQuote.model_validate(result["quote"])
            except (ValidationError, KeyError) as exc:
                task.status = "failed"
                task.last_error = "quote failed schema validation: %s" % str(exc)[:200]
                record_event(session, case, "insurance.quote.rejected_schema", "system", now, self._env(), {"task_id": task.id, "insurer_id": task.insurer_id})
                return
            task.status = "quoted"
            task.open_questions_json = []
            existing = [q for q in repo.current_quotes(session, case.id, task.needs_version) if q.insurer_id == task.insurer_id]
            for old in existing:
                if old.quote_ref == quote.quote_ref and old.quote_version == quote.quote_version:
                    return
                old.superseded_by = "replaced"
            row = self._store_quote(session, case, task, quote, now)
            record_event(session, case, "insurance.quote.received", "provider:" + task.insurer_id, now, self._env(),
                         {"quote_id": row.id, "insurer_id": task.insurer_id, "annual_premium_minor": quote.annual_premium_minor, "quote_version": quote.quote_version,
                          "valid_until": quote.valid_until.isoformat(), "task_id": task.id})
        elif status == "input_required":
            task.status = "input_required"
            task.open_questions_json = result.get("questions") or []
            record_event(session, case, "insurance.quote.input_required", "provider:" + task.insurer_id, now, self._env(),
                         {"task_id": task.id, "insurer_id": task.insurer_id, "question_ids": [q.get("id") for q in task.open_questions_json]})
        elif status == "declined":
            task.status = "declined"
            task.last_error = result.get("decline_reason")
            record_event(session, case, "insurance.quote.declined", "provider:" + task.insurer_id, now, self._env(), {"task_id": task.id, "insurer_id": task.insurer_id, "reason": task.last_error})
        elif status == "pending":
            task.status = "pending"
            enqueue_job(session, "poll_quote_task", {"task_id": task.id}, now + timedelta(seconds=5), dedupe_key="poll_quote_task:" + task.id)
        else:
            task.status = "failed"
            task.last_error = "unexpected provider status %r" % status

    def _store_quote(self, session: Session, case: m.Case, task: m.QuoteTask, quote: RentersQuote, now: datetime) -> m.InsuranceQuote:
        row = m.InsuranceQuote(
            id=new_id("quote"),
            case_id=case.id,
            quote_task_id=task.id,
            insurer_id=quote.insurer_id,
            needs_version=quote.needs_version,
            quote_version=quote.quote_version,
            quote_ref=quote.quote_ref,
            annual_premium_minor=quote.annual_premium_minor,
            currency=quote.currency,
            coverage_json=quote.coverage.model_dump(mode="json"),
            exclusions_json=[c.model_dump() for c in quote.exclusions],
            policy_form_version=quote.policy_form_version,
            valid_until=quote.valid_until,
            answers_hash=quote.answers_hash,
            status=quote.status,
            quote_json=quote.model_dump(mode="json", by_alias=True),
            created_at=now,
        )
        session.add(row)
        session.flush()
        return row

    def _supersede_quotes(self, session: Session, case: m.Case, needs_version: int, reason: str, now: datetime) -> None:
        for task in repo.quote_tasks_for_case(session, case.id, needs_version):
            if task.status in ACTIVE_TASK_STATUSES:
                task.status = "superseded"
                task.last_error = reason
                task.updated_at = now
        for quote in repo.current_quotes(session, case.id, needs_version):
            quote.superseded_by = "needs_v%d" % (needs_version + 1)
        self._invalidate_open_actions(session, case, reason, now)
        case.selected_quote_id = None

    def _invalidate_open_actions(self, session: Session, case: m.Case, reason: str, now: datetime) -> None:
        for action in repo.open_actions(session, case.id):
            action.status = "invalidated"
            action.result_json = {"reason": reason, "invalidated_at": now.isoformat()}
            action.updated_at = now
            if action.approval_id:
                approval = session.get(m.Approval, action.approval_id)
                if approval and approval.revoked_at is None and approval.consumed_at is None:
                    approval.revoked_at = now
            record_event(session, case, "insurance.action.invalidated", "system", now, self._env(), {"action_id": action.id, "reason": reason})
        if case.state == st.AWAITING_APPROVAL:
            app = session.get(m.Application, case.current_application_id) if case.current_application_id else None
            if app and app.status in ("prepared", "approved"):
                app.status = "superseded"
                app.updated_at = now

    def _settle_state(self, session: Session, case: m.Case, actor: str, now: datetime) -> None:
        """Derive the pre-submission case state from task statuses."""
        if case.state not in (st.COLLECTING, st.QUOTING, st.NEEDS_INFORMATION, st.COMPARING, st.AWAITING_SELECTION):
            return
        needs = repo.current_needs(session, case)
        tasks = [t for t in repo.quote_tasks_for_case(session, case.id, needs.version) if t.status != "superseded"]
        if not tasks:
            if case.state != st.COLLECTING:
                transition(session, case, st.COLLECTING, actor, "insurance.case.reset_to_collecting", now, self._env())
            return
        statuses = {t.status for t in tasks}
        # A comparison stays useful when one insurer is missing; the missing response is disclosed.
        if "quoted" in statuses:
            target = st.AWAITING_SELECTION
        elif "input_required" in statuses:
            target = st.NEEDS_INFORMATION
        elif statuses & {"pending", "timeout", "failed"}:
            target = st.QUOTING
        else:
            target = st.MANUAL_REVIEW
        if target == case.state:
            return
        if target == st.AWAITING_SELECTION:
            if case.state != st.COMPARING:
                if case.state == st.COLLECTING:
                    transition(session, case, st.QUOTING, actor, "insurance.quotes.requested", now, self._env())
                transition(session, case, st.COMPARING, actor, "insurance.comparison.ready", now, self._env(),
                           {"quoted": sorted(t.insurer_id for t in tasks if t.status == "quoted"),
                            "missing": sorted(t.insurer_id for t in tasks if t.status != "quoted")})
            transition(session, case, st.AWAITING_SELECTION, actor, "insurance.case.awaiting_selection", now, self._env())
            return
        if target == st.MANUAL_REVIEW:
            case.review_reason = "No insurer returned a usable quote: " + "; ".join("%s=%s" % (t.insurer_id, t.status) for t in tasks)
            transition(session, case, st.MANUAL_REVIEW, actor, "insurance.case.manual_review", now, self._env(), {"reason": case.review_reason})
            return
        if case.state == st.COLLECTING and target != st.QUOTING:
            transition(session, case, st.QUOTING, actor, "insurance.quotes.requested", now, self._env())
        transition(session, case, target, actor, "insurance.case.%s" % target, now, self._env())

    # ------------------------------------------------------------------ polling (worker)
    async def poll_quote_task(self, task_id: str) -> Dict[str, Any]:
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            if task is None or task.status in ("superseded", "quoted", "declined"):
                return {"skipped": True}
            case = repo.get_case(session, task.case_id)
            needs_row = session.get(m.InsuranceNeedsRow, task.needs_id)
            needs = InsuranceNeeds(**needs_row.data_json)
            answers = repo.answers_as_records(session, case.id)
            insurer_answers = {q["id"]: answers[q["id"]]["value"] for q in self.registry.questions(task.insurer_id) if q["id"] in answers and answers[q["id"]]["value"] != UNKNOWN}
            requirements = needs.to_requirements()
            requirements["correlation_id"] = task.correlation_id
            external, request_ref, insurer_id, case_id = task.external_task_id, task.request_ref, task.insurer_id, case.id
        adapter = self.registry.get(insurer_id)
        try:
            if external:
                result = await self.ctx.call_provider(case_id, insurer_id, "get_task", adapter.get_task(external), request_ref=request_ref)
            else:
                # Same request reference: an idempotent provider returns the original task.
                result = await self.ctx.call_provider(case_id, insurer_id, "request_quote", adapter.request_quote(requirements, request_ref, insurer_answers), request_ref=request_ref)
        except ProviderTimeout as exc:
            self._mark_task_uncertain(task_id, str(exc))
            return {"status": "timeout"}
        except ProviderMalformedResponse as exc:
            self._mark_task_failed(task_id, str(exc), retry=True)
            return {"status": "failed"}
        except (ProviderError, KeyError, ValueError) as exc:
            self._mark_task_failed(task_id, str(exc), retry=False)
            return {"status": "failed"}
        with self.db.session() as session:
            task = session.get(m.QuoteTask, task_id)
            case = repo.get_case(session, task.case_id)
            self._apply_task_result(session, case, task, result, self.now())
            self._settle_state(session, case, "worker", self.now())
            return {"status": task.status}

    # ------------------------------------------------------------------ comparison
    def comparison(self, case_id: str, customer_id: Optional[str]) -> Dict[str, Any]:
        with self.db.session() as session:
            case, needs = self._load(session, case_id, customer_id)
            quotes = [RentersQuote.model_validate(q.quote_json) for q in repo.current_quotes(session, case.id, needs.version)]
            quote_ids = {q.quote_ref: q.id for q in repo.current_quotes(session, case.id, needs.version)}
            missing = []
            for task in repo.quote_tasks_for_case(session, case.id, needs.version):
                if task.status in ("quoted", "superseded"):
                    continue
                missing.append(
                    {
                        "insurer_id": task.insurer_id,
                        "insurer_name": self.registry.config(task.insurer_id)["display_name"],
                        "status": task.status,
                        "disclosure": self._missing_disclosure(task),
                        "open_questions": task.open_questions_json or [],
                    }
                )
            result = compare_coverage(needs, quotes, self.now(), missing)
            for bucket in ("suitable", "excluded", "undetermined"):
                for entry in result[bucket]:
                    entry["quote_id"] = quote_ids.get(entry["quote_ref"])
            result["case_id"] = case.id
            result["case_status"] = case.state
            result["case_version"] = case.version
            result["environment"] = self._env()
            return result

    def _missing_disclosure(self, task: m.QuoteTask) -> str:
        name = self.registry.config(task.insurer_id)["display_name"]
        if task.status == "timeout":
            return "%s did not respond in time; its outcome is unknown and is being re-checked. The comparison below omits it." % name
        if task.status == "input_required":
            return "%s needs %d more answer(s) before it can quote." % (name, len(task.open_questions_json or []))
        if task.status == "declined":
            return "%s declined to quote: %s" % (name, task.last_error)
        if task.status == "failed":
            return "%s returned an unusable response (%s)." % (name, (task.last_error or "")[:120])
        return "%s has not responded yet." % name

    # ------------------------------------------------------------------ selection
    def select_quote(self, case_id: str, customer_id: Optional[str], quote_id: str, actor: str) -> Dict[str, Any]:
        now = self.now()
        with self.db.session() as session:
            case, needs = self._load(session, case_id, customer_id)
            quote = repo.get_quote(session, quote_id)
            if quote.case_id != case.id or quote.needs_version != needs.version or quote.superseded_by:
                raise CaseError("quote does not belong to the current needs version", 409)
            comparison = compare_coverage(needs, [RentersQuote.model_validate(quote.quote_json)], now)
            if not comparison["suitable"]:
                reasons = (comparison["excluded"] or comparison["undetermined"])[0]["reason"]
                raise CaseError("quote is not suitable: %s" % reasons, 409, {"reason": reasons})
            case.selected_quote_id = quote.id
            record_event(session, case, "insurance.quote.selected", actor, now, self._env(), {"quote_id": quote.id, "insurer_id": quote.insurer_id})
            return {"case_id": case.id, "selected_quote_id": quote.id, "version": case.version}

    # ------------------------------------------------------------------ views
    def _task_summary(self, task: m.QuoteTask) -> Dict[str, Any]:
        cfg = self.registry.config(task.insurer_id)
        return {
            "task_id": task.id,
            "insurer_id": task.insurer_id,
            "insurer_name": cfg["display_name"],
            "external_task_id": task.external_task_id,
            "correlation_id": task.correlation_id,
            "request_ref": task.request_ref,
            "status": task.status,
            "open_questions": task.open_questions_json or [],
            "last_error": task.last_error,
            "environment": task.environment,
            "updated_at": task.updated_at.isoformat(),
        }

    def outstanding_questions(self, session: Session, case: m.Case, needs: InsuranceNeeds) -> List[Dict[str, Any]]:
        """Questions the customer still needs to answer, in each insurer's own wording.

        Application questions are only surfaced for insurers whose quote is not already
        excluded, so the customer is not asked things that cannot change the outcome.
        """
        answers = repo.latest_answers(session, case.id)
        out: List[Dict[str, Any]] = []
        tasks = [t for t in repo.quote_tasks_for_case(session, case.id, needs.version) if t.status != "superseded"]
        excluded_insurers: set = set()
        quoted = [RentersQuote.model_validate(q.quote_json) for q in repo.current_quotes(session, case.id, needs.version)]
        if quoted:
            result = compare_coverage(needs, quoted, self.now())
            excluded_insurers = {e["insurer_id"] for e in result["excluded"]}
        # Only ask an insurer's application questions once it has quoted and is not excluded.
        insurers = [t.insurer_id for t in tasks if t.status == "quoted" and t.insurer_id not in excluded_insurers]
        for task in tasks:
            for q in task.open_questions_json or []:
                qid = q.get("id", "")
                if qid.startswith("needs:"):
                    out.append({"insurer_id": task.insurer_id, "question_id": qid, "text": q.get("text"), "type": "needs_field", "reason": "blocks_quote"})
                    continue
                if qid in answers and answers[qid].answer_json.get("value") != UNKNOWN:
                    continue
                out.append({"insurer_id": task.insurer_id, "insurer_name": self.registry.config(task.insurer_id)["display_name"], "question_id": qid,
                            "text": q.get("text"), "type": q.get("type", "boolean"), "choices": q.get("choices"), "reason": "blocks_quote",
                            "unknown_allowed": True})
        for insurer_id in insurers:
            for q in self.registry.questions(insurer_id):
                if any(o["question_id"] == q["id"] for o in out):
                    continue
                current = answers.get(q["id"])
                if current is None or current.answer_json.get("value") == UNKNOWN:
                    out.append({"insurer_id": insurer_id, "insurer_name": self.registry.config(insurer_id)["display_name"], "question_id": q["id"],
                                "text": q["text"], "type": q.get("type", "boolean"), "choices": q.get("choices"), "reason": "required_for_application",
                                "unknown_allowed": True})
        return out

    def case_view(self, case_id: str, customer_id: Optional[str]) -> Dict[str, Any]:
        with self.db.session() as session:
            case, needs = self._load(session, case_id, customer_id)
            quotes = repo.current_quotes(session, case.id, needs.version)
            tasks = [t for t in repo.quote_tasks_for_case(session, case.id, needs.version) if t.status != "superseded"]
            answers = repo.answers_as_records(session, case.id)
            actions = repo.actions_for_case(session, case.id)
            applications = repo.applications_for_case(session, case.id)
            policies = repo.policies_for_case(session, case.id)
            events = repo.events_for_case(session, case.id)
            pending_action = next((a for a in reversed(actions) if a.status in ("proposed", "approved", "executing")), None)
            current_app = next((a for a in applications if a.id == case.current_application_id), None)
            policy = policies[-1] if policies else None
            timeline = self._timeline(events, policy)
            view = {
                "id": case.id,
                "customer_id": case.customer_id,
                "status": case.state,
                "version": case.version,
                "environment": self._env(),
                "adapter_mode": self.registry.mode,
                "review_reason": case.review_reason,
                "needs": needs.model_dump(mode="json"),
                "missing_fields": needs.missing_fields(),
                "missing_for_comparison": needs.missing_fields(for_comparison=True),
                "confirmed_answers": answers,
                "outstanding_questions": self.outstanding_questions(session, case, needs),
                "quote_tasks": [self._task_summary(t) for t in tasks],
                "quotes": [self._quote_summary(q) for q in quotes],
                "selected_quote_id": case.selected_quote_id,
                "application": self._application_view(current_app) if current_app else None,
                "pending_action": self._action_view(pending_action, session) if pending_action else None,
                "actions": [self._action_view(a, session) for a in actions],
                "policy": self._policy_view(policy) if policy else None,
                "timeline": timeline,
                "next_decision": self._next_decision(case, needs, tasks, quotes, pending_action, policy),
                "updated_at": case.updated_at.isoformat(),
            }
            return view

    def _quote_summary(self, q: m.InsuranceQuote) -> Dict[str, Any]:
        return {
            "quote_id": q.id,
            "quote_ref": q.quote_ref,
            "quote_version": q.quote_version,
            "insurer_id": q.insurer_id,
            "insurer_name": q.quote_json.get("insurer_name"),
            "annual_premium_minor": q.annual_premium_minor,
            "annual_premium_display": fmt_minor(q.annual_premium_minor, q.currency),
            "currency": q.currency,
            "deductible_minor": q.coverage_json["deductible_minor"],
            "property_limit_minor": q.coverage_json["property_limit_minor"],
            "liability_limit_minor": q.coverage_json["liability_limit_minor"],
            "replacement_cost": q.coverage_json["replacement_cost"],
            "policy_form_version": q.policy_form_version,
            "valid_until": q.valid_until.isoformat(),
            "status": q.status,
            "source": q.quote_json.get("source"),
            "revision_reason": q.quote_json.get("revision_reason"),
            "quote": q.quote_json,
        }

    def _application_view(self, app: m.Application) -> Dict[str, Any]:
        return {
            "application_id": app.id,
            "quote_id": app.quote_id,
            "status": app.status,
            "revision": app.revision,
            "answers_hash": app.answers_hash,
            "payload_hash": app.payload_hash,
            "submission_ref": app.submission_ref,
            "payload": app.payload_json,
            "updated_at": app.updated_at.isoformat(),
        }

    def _action_view(self, action: m.Action, session: Session) -> Dict[str, Any]:
        approval = session.get(m.Approval, action.approval_id) if action.approval_id else None
        return {
            "action_id": action.id,
            "type": action.type,
            "status": action.status,
            "payload_hash": action.payload_hash,
            "challenge_id": action.challenge_id if action.status == "proposed" else None,
            "challenge_expires_at": action.challenge_expires_at.isoformat(),
            "expected_case_version": action.expected_case_version,
            "application_id": action.application_id,
            "provider_ref": action.provider_ref,
            "idempotency_key": action.idempotency_key,
            "review": action.payload_json.get("review"),
            "approval": {
                "approval_id": approval.id,
                "approver_id": approval.approver_id,
                "expires_at": approval.expires_at.isoformat(),
                "consumed_at": approval.consumed_at.isoformat() if approval.consumed_at else None,
                "revoked_at": approval.revoked_at.isoformat() if approval.revoked_at else None,
            }
            if approval
            else None,
            "result": action.result_json,
            "created_at": action.created_at.isoformat(),
        }

    def _policy_view(self, policy: m.IssuedPolicy) -> Dict[str, Any]:
        today = self.now().date()
        effective = policy.effective_at
        return {
            "policy_id": policy.id,
            "insurer_policy_ref": policy.insurer_policy_ref,
            "effective_at": effective.isoformat() if effective else None,
            "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
            "coverage_starts_in_future": bool(effective and effective > today),
            "coverage_label": ("issued – coverage starts %s" % effective.isoformat()) if effective and effective > today else "issued – in force",
            "verified": policy.verified,
            "verification": policy.verification_json,
            "declarations_document_id": policy.declarations_document_id,
            "declarations": policy.declarations_json,
        }

    @staticmethod
    def _timeline(events: List[m.CaseEvent], policy: Optional[m.IssuedPolicy]) -> List[Dict[str, Any]]:
        labels = {
            "insurance.case.created": "case opened",
            "insurance.quotes.requested": "quotes requested",
            "insurance.quote.received": "quoted",
            "insurance.quote.input_required": "insurer question",
            "insurance.quote.selected": "selected",
            "insurance.application.prepared": "application prepared",
            "insurance.action.approved": "approved by customer",
            "insurance.application.submitted": "submitted",
            "insurance.application.revised_offer": "revised offer",
            "insurance.policy.bound": "bound",
            "insurance.policy.issued": "issued",
            "insurance.policy.verified": "verified",
            "insurance.application.declined": "declined",
            "insurance.case.manual_review": "manual review",
            "insurance.case.completed": "completed",
        }
        out = []
        for e in events:
            if e.event_type in labels:
                out.append({"label": labels[e.event_type], "event_type": e.event_type, "at": e.occurred_at.isoformat(), "state": e.to_state, "data": e.data_json, "sequence": e.sequence})
        if policy and policy.effective_at:
            out.append({"label": "effective (coverage starts)", "event_type": "policy.effective", "at": policy.effective_at.isoformat(), "state": None, "data": {}, "sequence": 10**6})
        return out

    def _next_decision(self, case: m.Case, needs: InsuranceNeeds, tasks, quotes, pending_action, policy) -> str:
        if case.state == st.COLLECTING:
            missing = needs.missing_fields()
            return "Confirm the remaining interview fields: %s." % ", ".join(missing) if missing else "Request quotes from the three insurers."
        if case.state == st.NEEDS_INFORMATION:
            return "Answer the insurer question(s) shown; choose 'I don't know' if you are unsure. Answers are never guessed."
        if case.state == st.QUOTING:
            return "Waiting for insurer responses. A comparison is available as soon as at least one quote arrives."
        if case.state in (st.COMPARING, st.AWAITING_SELECTION):
            missing = needs.missing_fields(for_comparison=True)
            if missing:
                return "Confirm %s so quotes can be checked for suitability." % ", ".join(missing)
            return "Review the comparison and select a suitable quote."
        if case.state == st.AWAITING_APPROVAL and pending_action is not None:
            if pending_action.status == "approved":
                return "Approval recorded; the executor is submitting the application."
            return "Review the exact application and terms, then approve or reject %s." % pending_action.type.replace("_", " ")
        if case.state in (st.SUBMITTED, st.UNDERWRITING):
            return "The insurer is underwriting. No action needed; you will be asked again if the terms change."
        if case.state == st.REVISED_OFFER:
            return "Underwriting changed the terms. Review the revised offer before anything is bound."
        if case.state == st.BOUND:
            return "Policy bound; waiting for issued declarations to verify."
        if case.state == st.ISSUED:
            return "Policy issued; verification against the approved terms is in progress."
        if case.state == st.COMPLETED and policy is not None:
            return "Verified issued policy %s. Coverage starts %s." % (policy.insurer_policy_ref, policy.effective_at.isoformat() if policy.effective_at else "unknown")
        if case.state == st.DECLINED:
            return "The insurer declined. Select another suitable quote if one is available."
        if case.state == st.EXPIRED:
            return "Quotes expired. Request fresh quotes."
        if case.state == st.MANUAL_REVIEW:
            return "An operator is reviewing this case: %s" % (case.review_reason or "")
        return ""
