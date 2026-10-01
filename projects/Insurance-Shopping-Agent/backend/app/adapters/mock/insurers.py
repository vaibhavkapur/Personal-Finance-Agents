"""Deterministic mock insurer engine driven by the fixture files.

The engine runs without credentials, uses the shared controllable clock and supports
fault injection (timeouts, malformed responses, declines, delayed callbacks).
"""
from __future__ import annotations

import copy
import threading
import uuid
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Deque, Dict, List, Optional

from ...clock import Clock
from ...domain.application import answers_hash
from ...domain.hashing import sha256_hash
from ...domain.needs import UNKNOWN
from ...domain.quotes import QUOTE_SCHEMA
from ..base import ProviderMalformedResponse, ProviderTimeout, provider_source

FAULT_TIMEOUT = "timeout"  # raise timeout *after* recording the write (outcome unknown)
FAULT_TIMEOUT_BEFORE = "timeout_before"  # raise timeout before doing anything
FAULT_MALFORMED = "malformed"
FAULT_DECLINE_APPLICATION = "decline_next_application"
FAULT_EFFECTIVE_DATE_SHIFT = "effective_date_shift_days"
FAULT_DELAYED_CALLBACK = "delayed_callback"


class MockInsurer:
    def __init__(self, config: Dict[str, Any], clock: Clock, protocol: str = "direct") -> None:
        self.config = config
        self.insurer_id: str = config["insurer_id"]
        self.display_name: str = config["display_name"]
        self.environment: str = config.get("environment", "mock")
        self.protocol = protocol
        self.clock = clock
        self.behavior = config["behavior"]
        self.product = config["product"]
        self.policy_form = config["policy_form"]
        self.questions: List[Dict[str, Any]] = config["questions"]
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._tasks_by_request: Dict[str, str] = {}
        self._submissions: Dict[str, Dict[str, Any]] = {}
        self._submissions_by_request: Dict[str, str] = {}
        self._faults: Deque[Dict[str, Any]] = deque()
        self._callbacks: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self.call_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ utilities
    def now(self) -> datetime:
        return self.clock.now()

    def inject_fault(self, kind: str, operation: Optional[str] = None, **params: Any) -> None:
        """Queue a one-shot fault. `operation` limits it to one adapter operation."""
        with self._lock:
            self._faults.append({"kind": kind, "operation": operation, "params": params})

    def clear_faults(self) -> None:
        with self._lock:
            self._faults.clear()

    def pending_callbacks(self) -> List[Dict[str, Any]]:
        with self._lock:
            out, self._callbacks = self._callbacks, []
            return out

    def _take_fault(self, operation: str, kinds: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            for fault in list(self._faults):
                if fault["operation"] not in (None, operation):
                    continue
                if kinds and fault["kind"] not in kinds:
                    continue
                self._faults.remove(fault)
                return fault
        return None

    def _source(self) -> Dict[str, Any]:
        return provider_source(self.insurer_id, self.environment, self.protocol, self.now())

    def _log(self, operation: str, **data: Any) -> None:
        self.call_log.append({"operation": operation, "at": self.now().isoformat(), **data})

    def clause(self, clause_id: str) -> Dict[str, Any]:
        for c in self.policy_form["clauses"]:
            if c["id"] == clause_id:
                return {"clause_id": c["id"], "title": c["title"], "text": c["text"]}
        raise KeyError(clause_id)

    def policy_form_document(self) -> Dict[str, Any]:
        return {
            "insurer_id": self.insurer_id,
            "policy_form_version": self.policy_form["policy_form_version"],
            "clauses": copy.deepcopy(self.policy_form["clauses"]),
            "source": self._source(),
        }

    def question(self, question_id: str) -> Optional[Dict[str, Any]]:
        for q in self.questions:
            if q["id"] == question_id:
                return q
        return None

    # ------------------------------------------------------------------ quoting
    def _pre_fault(self, operation: str) -> None:
        fault = self._take_fault(operation, [FAULT_TIMEOUT_BEFORE, FAULT_MALFORMED])
        if fault is None:
            return
        if fault["kind"] == FAULT_TIMEOUT_BEFORE:
            raise ProviderTimeout("%s: %s timed out before the request was processed" % (self.insurer_id, operation))
        if fault["kind"] == FAULT_MALFORMED:
            raise _Malformed()

    def _post_fault(self, operation: str) -> None:
        fault = self._take_fault(operation, [FAULT_TIMEOUT])
        if fault is not None:
            raise ProviderTimeout("%s: %s accepted but the response was lost" % (self.insurer_id, operation))

    def request_quote(self, needs: Dict[str, Any], request_ref: str, answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._log("request_quote", request_ref=request_ref)
        try:
            self._pre_fault("request_quote")
        except _Malformed:
            return {"unexpected": "payload", "source": self._source()}
        with self._lock:
            if request_ref in self._tasks_by_request:
                task = self._tasks[self._tasks_by_request[request_ref]]
                return self._task_view(task)
            task_id = "%s-task-%s" % (self.config["label"].lower(), uuid.uuid4().hex[:10])
            task = {
                "task_id": task_id,
                "request_ref": request_ref,
                "context_id": needs.get("correlation_id") or request_ref,
                "needs": copy.deepcopy(needs),
                "answers": {},
                "status": "pending",
                "quote": None,
                "quote_version": 0,
                "decline_reason": None,
                "created_at": self.now().isoformat(),
            }
            for qid, value in (answers or {}).items():
                if self.question(qid) is not None and value not in (None, UNKNOWN):
                    task["answers"][qid] = value
            self._tasks[task_id] = task
            self._tasks_by_request[request_ref] = task_id
            self._evaluate_task(task)
            self._post_fault("request_quote")
            return self._task_view(task)

    def answer_question(self, task_ref: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        self._log("answer_question", task_ref=task_ref, question_id=answer.get("question_id"))
        self._pre_fault("answer_question")
        with self._lock:
            task = self._tasks.get(task_ref)
            if task is None:
                raise KeyError("unknown task %s" % task_ref)
            qid = answer.get("question_id")
            if self.question(qid) is None:
                raise ValueError("question %s does not belong to %s" % (qid, self.insurer_id))
            value = answer.get("value")
            if value in (None, UNKNOWN):
                # An unknown answer is recorded as unknown: it never resolves the question.
                task["answers"].pop(qid, None)
            else:
                task["answers"][qid] = value
            self._evaluate_task(task)
            self._post_fault("answer_question")
            return self._task_view(task)

    def get_task(self, task_ref: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_ref)
            if task is None:
                raise KeyError("unknown task %s" % task_ref)
            return self._task_view(task)

    def _evaluate_task(self, task: Dict[str, Any]) -> None:
        needs = task["needs"]
        missing_needs = [f for f in self.config["required_needs_fields"] if needs.get(f) in (None, UNKNOWN)]
        if missing_needs:
            task["status"] = "input_required"
            task["pending_questions"] = [
                {"id": "needs:" + f, "text": "Please provide %s." % f.replace("_", " "), "type": "needs_field", "required": True}
                for f in missing_needs
            ]
            return
        if needs["property_limit_minor"] > self.product["max_property_limit_minor"]:
            task["status"] = "declined"
            task["decline_reason"] = "Requested personal property limit exceeds the program maximum of %d minor units." % self.product[
                "max_property_limit_minor"
            ]
            return
        liability_options = {int(k): v for k, v in self.product["liability_options_minor"].items()}
        eligible_liability = [lim for lim in sorted(liability_options) if lim >= needs["liability_limit_minor"]]
        if not eligible_liability:
            task["status"] = "declined"
            task["decline_reason"] = "Requested liability limit is not offered."
            return
        follow_up = self.behavior.get("asks_followup_question_id")
        if follow_up and follow_up not in task["answers"]:
            task["status"] = "input_required"
            task["pending_questions"] = [self.question(follow_up)]
            return
        task["pending_questions"] = []
        if task["quote"] is None:
            task["quote"] = self._build_quote(task, eligible_liability[0], liability_options[eligible_liability[0]])
            task["quote_version"] = 1
        task["status"] = "quoted"

    def _build_quote(self, task: Dict[str, Any], liability_limit: int, liability_add: int, premium_override: Optional[int] = None,
                     version: int = 1, revision_reason: Optional[str] = None, quote_ref: Optional[str] = None) -> Dict[str, Any]:
        needs = task["needs"]
        product = self.product
        property_limit = max(int(needs["property_limit_minor"]), product["base_property_limit_minor"])
        extra_steps = max(0, (property_limit - product["base_property_limit_minor"]) // product["property_step_minor"])
        premium = product["base_annual_premium_minor"] + extra_steps * product["property_step_premium_minor"] + liability_add
        if premium_override is not None:
            premium = premium_override
        deductible = product["default_deductible_minor"]
        now = self.now()
        valid_until = now + timedelta(days=self.behavior["quote_validity_days"])
        item_classes = {
            name: {"status": t["status"], "sublimit_minor": t.get("sublimit_minor"), "clause_id": t["clause_id"]}
            for name, t in product["item_classes"].items()
        }
        citations = {
            "property_limit": self._first_clause_matching(("COV-A", "COV-C", "COV-PP")),
            "liability_limit": self._first_clause_matching(("COV-L", "COV-LIAB")),
            "deductible": self._first_clause_matching(("DED",)),
            "replacement_cost": self._first_clause_matching(("LOSS-RC",)),
        }
        cited = {cid for cid in citations.values() if cid}
        cited.update(t["clause_id"] for t in item_classes.values())
        quote = {
            "schema": QUOTE_SCHEMA,
            "quote_ref": quote_ref or "%s-q-%s" % (self.config["label"].lower(), uuid.uuid4().hex[:8]),
            "insurer_id": self.insurer_id,
            "insurer_name": self.display_name,
            "needs_version": int(needs.get("needs_version", 1)),
            "quote_version": version,
            "annual_premium_minor": premium,
            "currency": product["currency"],
            "coverage": {
                "property_limit_minor": property_limit,
                "liability_limit_minor": liability_limit,
                "deductible_minor": deductible,
                "replacement_cost": product["replacement_cost"],
                "item_classes": item_classes,
                "endorsements": [self.clause(cid) for cid in product["endorsements"]],
                "citations": citations,
            },
            "exclusions": [self.clause(cid) for cid in product["exclusions"]],
            "policy_form_version": self.policy_form["policy_form_version"],
            "quoted_at": now.isoformat(),
            "valid_until": valid_until.isoformat(),
            "effective_date": needs["effective_date"],
            "answers_hash": answers_hash([{"question_id": k, "value": v} for k, v in task["answers"].items()]),
            "status": "quoted",
            "source": self._source(),
            "revision_reason": revision_reason,
            "clauses": [self.clause(cid) for cid in sorted(cited)],
        }
        return quote

    def _first_clause_matching(self, suffixes) -> Optional[str]:
        for c in self.policy_form["clauses"]:
            for suffix in suffixes:
                if c["id"].endswith(suffix):
                    return c["id"]
        return None

    def _task_view(self, task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "task_ref": task["task_id"],
            "context_id": task["context_id"],
            "status": task["status"],
            "questions": copy.deepcopy(task.get("pending_questions", [])),
            "application_questions": copy.deepcopy(self.questions),
            "answered_question_ids": sorted(task["answers"].keys()),
            "quote": copy.deepcopy(task["quote"]) if task["status"] == "quoted" else None,
            "decline_reason": task.get("decline_reason"),
            "source": self._source(),
        }

    # ------------------------------------------------------------------ applications
    def submit_application(self, payload: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        self._log("submit_application", request_ref=request_ref)
        self._pre_fault("submit_application")
        with self._lock:
            if request_ref in self._submissions_by_request:
                return self._submission_view(self._submissions[self._submissions_by_request[request_ref]])
            task = self._find_task_by_quote(payload.get("quote_ref"))
            if task is None:
                raise ValueError("unknown quote_ref %s" % payload.get("quote_ref"))
            quote = task["quote"]
            if payload.get("quote_version") != quote["quote_version"]:
                raise ValueError("quote version mismatch: %s vs %s" % (payload.get("quote_version"), quote["quote_version"]))
            if datetime.fromisoformat(quote["valid_until"]) < self.now():
                raise ValueError("quote %s has expired" % quote["quote_ref"])
            answers = {a["question_id"]: a["value"] for a in payload.get("answers", [])}
            missing = [q["id"] for q in self.questions if q.get("required", True) and answers.get(q["id"]) in (None, UNKNOWN)]
            if missing:
                raise ValueError("application incomplete; unanswered: %s" % ", ".join(missing))
            submission_ref = "%s-sub-%s" % (self.config["label"].lower(), uuid.uuid4().hex[:10])
            decline_fault = self._take_fault("submit_application", [FAULT_DECLINE_APPLICATION])
            shift_fault = self._take_fault("submit_application", [FAULT_EFFECTIVE_DATE_SHIFT])
            submission = {
                "submission_ref": submission_ref,
                "request_ref": request_ref,
                "task_id": task["task_id"],
                "payload": copy.deepcopy(payload),
                "answers": answers,
                "status": "underwriting",
                "submitted_at": self.now().isoformat(),
                "completes_at": self.now() + timedelta(minutes=self.behavior["underwriting_duration_minutes"]),
                "revised_quote": None,
                "revision_accepted": False,
                "policy": None,
                "decline_reason": None,
                "forced_decline": decline_fault is not None,
                "effective_shift_days": (shift_fault or {}).get("params", {}).get("days", 0),
                "premium_minor": quote["annual_premium_minor"],
                "quote_version": quote["quote_version"],
            }
            self._submissions[submission_ref] = submission
            self._submissions_by_request[request_ref] = submission_ref
            self._schedule_callback_if_requested(submission)
            self._post_fault("submit_application")
            return self._submission_view(submission)

    def accept_revised_offer(self, submission_ref: str, quote_ref: str, quote_version: int, request_ref: str) -> Dict[str, Any]:
        self._log("accept_revised_offer", submission_ref=submission_ref, request_ref=request_ref)
        self._pre_fault("accept_revised_offer")
        with self._lock:
            submission = self._submissions.get(submission_ref)
            if submission is None:
                raise KeyError("unknown submission %s" % submission_ref)
            if submission.get("accept_request_ref") == request_ref:
                return self._submission_view(submission)
            revised = submission.get("revised_quote")
            if submission["status"] != "revised_offer" or revised is None:
                raise ValueError("submission %s has no open revised offer" % submission_ref)
            if revised["quote_ref"] != quote_ref or revised["quote_version"] != quote_version:
                raise ValueError("revised offer reference mismatch")
            submission["revision_accepted"] = True
            submission["accept_request_ref"] = request_ref
            submission["status"] = "underwriting"
            submission["completes_at"] = self.now() + timedelta(minutes=5)
            submission["premium_minor"] = revised["annual_premium_minor"]
            submission["quote_version"] = revised["quote_version"]
            self._post_fault("accept_revised_offer")
            return self._submission_view(submission)

    def get_policy_status(self, request_ref: str) -> Dict[str, Any]:
        self._log("get_policy_status", request_ref=request_ref)
        self._pre_fault("get_policy_status")
        with self._lock:
            submission_ref = self._submissions_by_request.get(request_ref)
            if submission_ref is None:
                return {"status": "not_found", "request_ref": request_ref, "source": self._source()}
            submission = self._submissions[submission_ref]
            self._advance_underwriting(submission)
            return self._submission_view(submission)

    def get_submission(self, submission_ref: str) -> Dict[str, Any]:
        with self._lock:
            submission = self._submissions[submission_ref]
            self._advance_underwriting(submission)
            return self._submission_view(submission)

    def _advance_underwriting(self, submission: Dict[str, Any]) -> None:
        if submission["status"] != "underwriting" or self.now() < submission["completes_at"]:
            return
        task = self._tasks[submission["task_id"]]
        answers = submission["answers"]
        decline_rule = self.behavior.get("decline_in_underwriting")
        if submission["forced_decline"] or (decline_rule and answers.get(decline_rule["when_answer"]["question_id"]) == decline_rule["when_answer"]["equals"]):
            submission["status"] = "declined"
            submission["decline_reason"] = decline_rule["reason"] if decline_rule and not submission["forced_decline"] else "Declined by underwriting (injected fault)."
            return
        revise_rule = self.behavior.get("revise_premium_in_underwriting")
        if revise_rule and not submission["revision_accepted"] and answers.get(revise_rule["when_answer"]["question_id"]) == revise_rule["when_answer"]["equals"]:
            base_quote = task["quote"]
            liability_limit = base_quote["coverage"]["liability_limit_minor"]
            revised = self._build_quote(
                task,
                liability_limit,
                0,
                premium_override=base_quote["annual_premium_minor"] + revise_rule["premium_delta_minor"],
                version=base_quote["quote_version"] + 1,
                revision_reason=revise_rule["reason"],
                quote_ref=base_quote["quote_ref"],
            )
            submission["revised_quote"] = revised
            submission["status"] = "revised_offer"
            return
        submission["status"] = "issued"
        submission["policy"] = self._issue_policy(submission)

    def _issue_policy(self, submission: Dict[str, Any]) -> Dict[str, Any]:
        task = self._tasks[submission["task_id"]]
        quote = submission["revised_quote"] if submission["revision_accepted"] and submission["revised_quote"] else task["quote"]
        payload = submission["payload"]
        effective = date.fromisoformat(quote["effective_date"]) + timedelta(days=int(submission.get("effective_shift_days") or 0))
        expires = date(effective.year + 1, effective.month, effective.day)
        policy_ref = "%s-POL-%s" % (self.config["label"], uuid.uuid4().hex[:8].upper())
        declarations = {
            "schema": "renters-declarations/v1",
            "insurer_id": self.insurer_id,
            "insurer_name": self.display_name,
            "insurer_policy_ref": policy_ref,
            "insured_name": (payload.get("applicant") or {}).get("display_name"),
            "address": (payload.get("applicant") or {}).get("address"),
            "property_limit_minor": quote["coverage"]["property_limit_minor"],
            "liability_limit_minor": quote["coverage"]["liability_limit_minor"],
            "deductible_minor": quote["coverage"]["deductible_minor"],
            "annual_premium_minor": submission["premium_minor"],
            "currency": quote["currency"],
            "replacement_cost": quote["coverage"]["replacement_cost"],
            "policy_form_version": quote["policy_form_version"],
            "effective_at": effective.isoformat(),
            "expires_at": expires.isoformat(),
            "exclusions": sorted(c["clause_id"] for c in quote["exclusions"]),
            "endorsements": sorted(c["clause_id"] for c in quote["coverage"]["endorsements"]),
            "issued_at": self.now().isoformat(),
            "bound_at": self.now().isoformat(),
        }
        declarations["document_id"] = "doc-" + sha256_hash(declarations)[7:23]
        return declarations

    def _submission_view(self, submission: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "submission_ref": submission["submission_ref"],
            "task_ref": submission["task_id"],
            "status": submission["status"],
            "revised_quote": copy.deepcopy(submission["revised_quote"]) if submission["status"] == "revised_offer" else None,
            "policy": copy.deepcopy(submission["policy"]) if submission["status"] == "issued" else None,
            "decline_reason": submission.get("decline_reason"),
            "underwriting_completes_at": submission["completes_at"].isoformat() if submission["status"] == "underwriting" else None,
            "source": self._source(),
        }

    def _find_task_by_quote(self, quote_ref: Optional[str]) -> Optional[Dict[str, Any]]:
        for task in self._tasks.values():
            if task["quote"] and task["quote"]["quote_ref"] == quote_ref:
                return task
        return None

    def _schedule_callback_if_requested(self, submission: Dict[str, Any]) -> None:
        fault = self._take_fault("submit_application", [FAULT_DELAYED_CALLBACK])
        if fault is None:
            return
        self._callbacks.append(
            {
                "event_id": "%s-evt-%s" % (self.config["label"].lower(), uuid.uuid4().hex[:8]),
                "type": "underwriting.status",
                "submission_ref": submission["submission_ref"],
                "request_ref": submission["request_ref"],
                "deliver_after": (self.now() + timedelta(seconds=fault["params"].get("delay_seconds", 0))).isoformat(),
            }
        )


class _Malformed(Exception):
    pass


def build_mock_insurers(configs: List[Dict[str, Any]], clock: Clock, protocol: str = "direct") -> Dict[str, MockInsurer]:
    return {cfg["insurer_id"]: MockInsurer(cfg, clock, protocol=protocol) for cfg in configs}
