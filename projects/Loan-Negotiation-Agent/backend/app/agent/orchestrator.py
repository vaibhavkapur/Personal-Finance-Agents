"""Agent orchestrator.

The planner (rules-based by default, optionally an LLM behind the same
interface) chooses which typed tool to call next. The orchestrator enforces the
tool-call budget, persists every turn, and escalates unresolved contradictions
with evidence. Calculations, authorization and state transitions never happen
here; they live in ``workflows`` and ``domain``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from sqlalchemy.orm import Session

from ..domain.money import format_minor
from ..persistence.models import Case, Customer
from ..workflows import states as st
from ..workflows.case_service import CaseService
from ..workflows.states import WorkflowError, record_event, transition
from .tools import ToolResponse, Tools


@dataclass
class PlanStep:
    kind: str  # tool|reply|escalate
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnContext:
    case_view: Dict[str, Any]
    message: str
    intent: Optional[str]
    params: Dict[str, Any]
    lenders: Dict[str, Dict[str, Any]]
    tool_results: List[ToolResponse] = field(default_factory=list)
    resolved_intent: Optional[str] = None
    notes: List[str] = field(default_factory=list)


class Planner(Protocol):
    name: str

    def next_step(self, ctx: TurnContext) -> PlanStep: ...

    def cost_usd(self) -> float: ...


# ------------------------------------------------------------------ helpers
def _pct(rate: Optional[str]) -> str:
    if rate is None:
        return "n/a"
    from decimal import Decimal

    return f"{(Decimal(rate) * 100).normalize()}%"


def explain_comparison(view: Dict[str, Any]) -> str:
    comparison = view.get("comparison")
    if not comparison:
        return "No comparison has been run yet."
    keep = comparison["keep"]
    horizon = comparison["horizon_months"]
    lines = [
        f"Over your {horizon}-month horizon, keeping the current loan ({_pct(keep['note_rate_decimal'])}) costs {format_minor(keep['economic_cost_at_horizon_minor'])} in interest and fees, with {format_minor(keep['remaining_balance_at_horizon_minor'])} still owed at month {horizon}. Monthly P&I: {format_minor(keep['monthly_pi_minor'])}."
    ]
    for o in comparison["offers"]:
        if not o["rankable"]:
            reason = "expired" if o.get("expired") else ("missing " + ", ".join(o.get("missing_fields", [])) if o.get("missing_fields") else "; ".join(o.get("contradictions", [])) or "incomplete")
            lines.append(f"- {o['label']}: not ranked ({reason}).")
            continue
        diff = o["economic_difference_vs_keep_minor"]
        direction = "less" if diff < 0 else "more"
        monthly = o["monthly_change_vs_keep_minor"]
        monthly_txt = f"{format_minor(abs(monthly))} {'lower' if monthly < 0 else 'higher'} monthly payment"
        fees = f"{format_minor(o['upfront_incremental_costs_minor'])} upfront costs" + (f" plus {format_minor(o['financed_costs_minor'])} financed" if o["financed_costs_minor"] else "")
        be = f"economic break-even at month {o['economic_break_even_month']}" if o["economic_break_even_month"] else "never breaks even"
        lines.append(
            f"- {o['label']}: {monthly_txt}; {fees}; {format_minor(o['remaining_balance_at_horizon_minor'])} still owed at month {horizon}; total {format_minor(abs(diff))} {direction} than keeping; {be}."
        )
        for note in o.get("notes", []):
            lines.append(f"    note: {note}")
    rec = comparison["recommendation"]
    lines.append(f"Assessment: {rec['reason']}")
    lines.append("These are estimates from your documents, not a lender commitment. Prepaids and escrow deposits are shown separately in cash-to-close and are not counted as cost.")
    return "\n".join(lines)


def _parse_months(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(months?|mos?)\b", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?)\b", text, re.I)
    if m:
        return int(round(float(m.group(1)) * 12))
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    m = re.search(r"\b(" + "|".join(words) + r")\s*(more\s+)?(years?|yrs?)\b", text, re.I)
    if m:
        return words[m.group(1).lower()] * 12
    return None


def _parse_money_minor(text: str) -> Optional[int]:
    m = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", text)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


class RulesPlanner:
    """Deterministic planner: keyword intents + case state -> tool calls."""

    name = "rules-planner-v1"

    def cost_usd(self) -> float:
        return 0.0

    # ---------------------------------------------------------------- intent
    def resolve_intent(self, ctx: TurnContext) -> str:
        if ctx.intent:
            return ctx.intent
        text = ctx.message.lower()
        if any(k in text for k in ("keep my", "keep the", "stay with", "don't refinance", "do not refinance", "keep current")):
            return "keep"
        if any(k in text for k in ("send the documents", "share my", "provide the", "release", "send them the", "pay stub", "w-2", "w2", "proof of income")):
            return "documents"
        if any(k in text for k in ("close", "accept the final", "accept final", "proceed to closing")):
            return "close"
        if any(k in text for k in ("apply", "application", "go ahead with", "proceed with", "submit")):
            return "apply"
        if any(k in text for k in ("ask", "negotiate", "better terms", "reprice", "match", "counter", "lender whether", "beat")):
            return "negotiate"
        if any(k in text for k in ("worth it", "compare", "should i refinance", "is refinancing", "run the numbers", "recalculate")):
            return "compare"
        if any(k in text for k in ("escrow", "balance", "correct", "confirm", "horizon", "stay for", "expect to stay", "months", "years", "cash to close", "finance the", "roll the", "into the loan", "pay the costs", "out of pocket")):
            return "answer"
        if any(k in text for k in ("status", "where are we", "what's next", "what next", "explain", "summary")):
            return "status"
        return "status"

    def _facts_from_text(self, ctx: TurnContext) -> Dict[str, Any]:
        text = ctx.message
        low = text.lower()
        facts: Dict[str, Any] = dict(ctx.params.get("facts", {}))
        months = _parse_months(text)
        if months and "holding_horizon_months" not in facts and any(k in low for k in ("stay", "keep", "horizon", "expect", "plan", "more")):
            facts["holding_horizon_months"] = months
        if "escrow" in low and "payment_includes_escrow" not in facts:
            if re.search(r"\b(not|no|doesn't|does not|excludes?|without)\b", low):
                facts["payment_includes_escrow"] = False
            elif re.search(r"\b(yes|includes?|including|with|does)\b", low):
                facts["payment_includes_escrow"] = True
        if re.search(r"\b(balance|statement)\b.*\b(correct|right|accurate|confirm(ed)?|yes)\b", low) or re.search(r"\b(yes|correct|confirm(ed)?)\b.*\bbalance\b", low):
            facts.setdefault("current_balance_confirmed", True)
        if "cash" in low or "to close" in low:
            amount = _parse_money_minor(text)
            if amount is not None:
                facts["maximum_cash_to_close_minor"] = amount
        if any(k in low for k in ("finance the", "roll the", "into the loan", "finance closing")):
            facts.setdefault("finance_costs_all", True)
        elif any(k in low for k in ("pay the costs", "out of pocket", "pay costs in cash", "don't finance", "do not finance")):
            facts.setdefault("finance_costs_all", False)
        return facts

    def _lenders_in_text(self, ctx: TurnContext) -> List[str]:
        """Lender ids mentioned in the message, in order of appearance."""
        low = ctx.message.lower()
        found = []
        for lid, lender in ctx.lenders.items():
            first = lender["name"].split(" ")[0].lower()
            positions = [p for p in (low.find(first), low.find(lid)) if p >= 0]
            if positions:
                found.append((min(positions), lid))
        if any(k in low for k in ("my lender", "my servicer", "current lender", "current servicer", "existing lender")):
            servicer = ctx.case_view["mortgage"].get("servicer_lender_id")
            if servicer and servicer not in [l for _, l in found]:
                found.append((low.find("my lender") if "my lender" in low else 0, servicer))
        return [lid for _, lid in sorted(found)]

    def _lender_from_text(self, ctx: TurnContext) -> Optional[str]:
        if ctx.params.get("lender_id"):
            return ctx.params["lender_id"]
        mentioned = self._lenders_in_text(ctx)
        return mentioned[0] if mentioned else None

    def _competing_from_text(self, ctx: TurnContext, lender_id: str) -> Optional[str]:
        if ctx.params.get("competing_offer_id"):
            return ctx.params["competing_offer_id"]
        mentioned = [l for l in self._lenders_in_text(ctx) if l != lender_id]
        if not mentioned:
            return None
        cands = [o for o in ctx.case_view["offers"] if o["lender_id"] == mentioned[0] and o["status"] in ("indicative_quote", "revised_quote")]
        return sorted(cands, key=lambda o: -o["version"])[0]["id"] if cands else None

    def _offer_from_text(self, ctx: TurnContext) -> Optional[str]:
        if ctx.params.get("offer_id"):
            return ctx.params["offer_id"]
        lender_id = self._lender_from_text(ctx)
        offers = [o for o in ctx.case_view["offers"] if o["status"] in ("indicative_quote", "revised_quote", "final_offer")]
        if lender_id:
            cands = [o for o in offers if o["lender_id"] == lender_id]
            if cands:
                return sorted(cands, key=lambda o: -o["version"])[0]["id"]
        rec = (ctx.case_view.get("comparison") or {}).get("recommendation") or {}
        if any(k in ctx.message.lower() for k in ("best", "recommended", "cheapest", "that offer", "the offer")) and rec.get("best_offer_id"):
            return rec["best_offer_id"]
        return rec.get("best_offer_id") if len(offers) == 1 else None

    # ------------------------------------------------------------------ plan
    def next_step(self, ctx: TurnContext) -> PlanStep:
        view = ctx.case_view
        state = view["status"]
        if ctx.resolved_intent is None:
            ctx.resolved_intent = self.resolve_intent(ctx)
        intent = ctx.resolved_intent
        done = [r.tool for r in ctx.tool_results]
        last = ctx.tool_results[-1] if ctx.tool_results else None

        if last is not None and not last.ok:
            return PlanStep(kind="reply", reply=f"I could not complete that step: {last.error}. {view['next_decision']['text']}")

        if state in st.TERMINAL_STATES:
            return PlanStep(kind="reply", reply=f"This case is finished with outcome '{state}'. {self._completion_note(view)}")
        if state == st.MANUAL_REVIEW:
            return PlanStep(kind="reply", reply="An operator is reviewing an uncertain provider outcome or contradiction on this case. I will not take further external actions until it is resolved.")

        # --- answering questions / collecting facts
        if intent == "answer":
            facts = self._facts_from_text(ctx)
            if "finance_costs_all" in facts:
                flag = bool(facts.pop("finance_costs_all"))
                facts["finance_costs"] = {o["id"]: flag for o in view["offers"] if o["status"] in ("indicative_quote", "revised_quote")}
            if facts and "record_borrower_facts" not in done:
                return PlanStep(kind="tool", tool="record_borrower_facts", args={"facts": facts})
            if not facts and not done:
                return PlanStep(kind="reply", reply="I did not catch a confirmable fact in that. " + self._ask_next(view))
            if not view["missing_fields"] and state in (st.COLLECTING, st.COMPARING) and "compare_loan_scenarios" not in done:
                return PlanStep(kind="tool", tool="compare_loan_scenarios", args={})
            if view["missing_fields"]:
                return PlanStep(kind="reply", reply="Recorded. " + self._ask_next(view))
            return PlanStep(kind="reply", reply="Recorded.\n" + explain_comparison(view) + "\n" + self._offer_next_steps(view))

        if intent == "compare":
            facts = self._facts_from_text(ctx)
            facts.pop("finance_costs_all", None)
            if facts and "record_borrower_facts" not in done:
                return PlanStep(kind="tool", tool="record_borrower_facts", args={"facts": facts})
            if view["missing_fields"]:
                return PlanStep(kind="reply", reply="Before I compare I need a few confirmations. " + self._ask_next(view))
            if "compare_loan_scenarios" not in done and state in (st.COLLECTING, st.COMPARING, st.AWAITING_DECISION):
                horizon = ctx.params.get("horizon_months") or _parse_months(ctx.message)
                return PlanStep(kind="tool", tool="compare_loan_scenarios", args={"horizon_months": horizon} if horizon else {})
            return PlanStep(kind="reply", reply=explain_comparison(view) + "\n" + self._offer_next_steps(view))

        if intent == "keep":
            if state != st.AWAITING_DECISION:
                return PlanStep(kind="reply", reply=f"I can record that decision once the comparison is complete (case is {state}). {view['next_decision']['text']}")
            return PlanStep(kind="escalate", data={"decision": "keep_current"}, reply="Recorded: you are keeping your current mortgage. " + explain_comparison(view))

        if intent == "negotiate":
            if "prepare_lender_request" in done:
                return PlanStep(kind="reply", reply=self._review_reply(last.data), data={"review_screen": last.data})
            if state != st.AWAITING_DECISION:
                return PlanStep(kind="reply", reply=f"A lender request can be prepared once the comparison is complete and no other action is pending (case is {state}). {view['next_decision']['text']}")
            lender_id = self._lender_from_text(ctx)
            if lender_id is None:
                names = ", ".join(f"{l['name']} ({lid})" for lid, l in ctx.lenders.items())
                return PlanStep(kind="reply", reply=f"Which lender should I ask? Options: {names}.")
            contradiction = self._contradiction_for_lender(view, lender_id)
            if contradiction:
                return PlanStep(kind="reply", reply=f"I cannot cite the {ctx.lenders[lender_id]['name']} offer while it has a contradiction: {contradiction}. Please supply corrected terms or ask the lender for a corrected Loan Estimate.")
            if "prepare_lender_request" not in done:
                args = {"lender_id": lender_id, "request_type": ctx.params.get("request_type", "reprice")}
                competing = self._competing_from_text(ctx, lender_id)
                if competing:
                    args["competing_offer_id"] = competing
                if ctx.params.get("disclosed_document_ids"):
                    args["disclosed_document_ids"] = ctx.params["disclosed_document_ids"]
                return PlanStep(kind="tool", tool="prepare_lender_request", args=args)
            screen = last.data
            return PlanStep(kind="reply", reply=self._review_reply(screen), data={"review_screen": screen})

        if intent == "apply":
            if "prepare_refinance_application" in done:
                return PlanStep(kind="reply", reply=self._review_reply(last.data), data={"review_screen": last.data})
            if state != st.AWAITING_DECISION:
                return PlanStep(kind="reply", reply=f"An application can be prepared once the comparison is complete and no other action is pending (case is {state}). {view['next_decision']['text']}")
            offer_id = self._offer_from_text(ctx)
            if offer_id is None:
                return PlanStep(kind="reply", reply="Which offer should I prepare the application for? " + self._list_offers(view))
            offer = next((o for o in view["offers"] if o["id"] == offer_id), None)
            if offer and offer["normalized"].get("contradictions"):
                return PlanStep(kind="escalate", data={"manual_review": True, "reason": "borrower asked to apply with a contradictory offer", "offer_id": offer_id, "evidence": offer["normalized"]["contradictions"]}, reply=f"That offer has an unresolved contradiction ({'; '.join(offer['normalized']['contradictions'])}). I have escalated it for review rather than applying on unsupported terms.")
            if "prepare_refinance_application" not in done:
                return PlanStep(kind="tool", tool="prepare_refinance_application", args={"offer_id": offer_id, "document_ids": ctx.params.get("document_ids", [])})
            return PlanStep(kind="reply", reply=self._review_reply(last.data), data={"review_screen": last.data})

        if intent == "documents":
            if state != st.CONDITIONS_OUTSTANDING:
                return PlanStep(kind="reply", reply=f"No lender is currently waiting for documents (case is {state}).")
            docs = ctx.params.get("document_ids") or []
            if not docs:
                return PlanStep(kind="reply", reply="Tell me which document ids to release (for example your income evidence document). Nothing is sent until you approve the review screen.")
            return PlanStep(kind="escalate", data={"document_release": docs}, reply="")

        if intent == "close":
            if state not in (st.FINAL_REVIEW,):
                return PlanStep(kind="reply", reply=f"Closing can only be requested after the final-term review (case is {state}). {view['next_decision']['text']}")
            app = next((a for a in view["applications"] if a["final_terms"]), None)
            if app is None:
                return PlanStep(kind="reply", reply="No final terms are recorded yet.")
            if "diff_final_terms" not in done:
                return PlanStep(kind="tool", tool="diff_final_terms", args={"application_id": app["id"]})
            return PlanStep(kind="escalate", data={"closing_request": app["id"], "review": last.data}, reply="")

        # --- status / default
        if state == st.COLLECTING:
            return PlanStep(kind="reply", reply=self._ask_next(view))
        if state == st.AWAITING_DECISION:
            return PlanStep(kind="reply", reply=explain_comparison(view) + "\n" + self._offer_next_steps(view))
        if state == st.FINAL_REVIEW:
            app = next((a for a in view["applications"] if a["final_terms"]), None)
            if app and "diff_final_terms" not in done:
                return PlanStep(kind="tool", tool="diff_final_terms", args={"application_id": app["id"]})
            return PlanStep(kind="reply", reply=self._final_review_reply(view, last.data if last else {}))
        return PlanStep(kind="reply", reply=f"Case is {state}. {view['next_decision']['text']} " + self._pending_note(view))

    # ------------------------------------------------------------- phrasing
    def _ask_next(self, view: Dict[str, Any]) -> str:
        qs = [q for q in view["outstanding_questions"] if q["field"] in view["missing_fields"]]
        if not qs:
            others = [q["question"] for q in view["outstanding_questions"] if q.get("kind") in ("offer_field", "contradiction")]
            base = "I have what I need to run the comparison." if not others else "I can compare now, but note: " + " ".join(others)
            return base
        return " ".join(q["question"] for q in qs[:2])

    def _offer_next_steps(self, view: Dict[str, Any]) -> str:
        extra = [q["question"] for q in view["outstanding_questions"] if q.get("kind") in ("offer_field", "contradiction", "lender_fact_request")]
        parts = ["Next you can: keep your current loan, ask a lender for better terms (I will draft the message for your approval), or apply with an offer (mock application, also needs your approval)."]
        if extra:
            parts.append("Open items: " + " ".join(extra))
        return " ".join(parts)

    def _list_offers(self, view: Dict[str, Any]) -> str:
        items = [f"{o['lender_name']} v{o['version']} ({o['id']}): {_pct(o['note_rate_decimal'])} / {o['term_months']} months, {o['status']}" for o in view["offers"] if o["status"] not in ("superseded", "refused", "expired")]
        return "; ".join(items)

    def _contradiction_for_lender(self, view: Dict[str, Any], lender_id: str) -> Optional[str]:
        for o in view["offers"]:
            if o["lender_id"] == lender_id and o["status"] in ("indicative_quote", "revised_quote") and o["normalized"].get("contradictions"):
                return "; ".join(o["normalized"]["contradictions"])
        return None

    def _review_reply(self, screen: Dict[str, Any]) -> str:
        review = screen.get("review", {})
        dest = review.get("destination", {})
        lines = [f"Ready for your approval: {review.get('title', 'action')}.", f"Destination: {dest.get('lender_name')} ({dest.get('environment')})."]
        if review.get("message_preview"):
            lines.append("Message:\n" + review["message_preview"])
        if review.get("terms"):
            t = review["terms"]
            lines.append(f"Terms: {_pct(t.get('note_rate_decimal'))} for {t.get('term_months')} months, principal {format_minor(t.get('principal_minor') or 0)}, monthly P&I {format_minor(t.get('monthly_pi_minor') or 0)}, status {t.get('status')}.")
        docs = review.get("documents_shared") or []
        lines.append("Documents shared: " + (", ".join(d["id"] for d in docs) if docs else "none") + ".")
        lines.append(f"Effect: {review.get('irreversible_effect', '')}")
        lines.append(f"Approve with challenge {screen.get('approval_challenge_id')} (expires {screen.get('challenge_expires_at')}), payload hash {screen.get('action_payload_hash')}, case version {screen.get('expected_case_version')}. Nothing is sent until you approve.")
        return "\n".join(lines)

    def _final_review_reply(self, view: Dict[str, Any], diff: Dict[str, Any]) -> str:
        review = (diff or {}).get("review") or {}
        if not review:
            return "Final terms are pending review."
        lines = [f"Final-term review: {review.get('summary')}"]
        for d in review.get("differences", []):
            lines.append(f"- {d['field']}: approved {d['earlier']} -> final {d['final']}" + (" (material)" if d["material"] else ""))
        if review.get("requires_reapproval"):
            lines.append("Because material terms changed, your earlier approval no longer applies. The comparison was refreshed with the final terms; review it before deciding to accept and request mock closing.")
        else:
            lines.append("No material changes. You can request mock closing; it still requires your approval and produces a closing record, not a funded loan.")
        return "\n".join(lines)

    def _pending_note(self, view: Dict[str, Any]) -> str:
        pending = view.get("pending_actions") or []
        if not pending:
            return ""
        a = pending[0]
        return f"Pending action {a['action_id']} ({a['action_type']}) is {a['status']}."

    def _completion_note(self, view: Dict[str, Any]) -> str:
        if view["status"] == st.MOCK_CLOSED:
            app = next((a for a in view["applications"] if a.get("closing_evidence")), None)
            return f"Mock closing evidence: {app['closing_evidence'].get('closing_record_id') if app else 'n/a'}. No real funds moved." if app else ""
        if view["status"] == st.KEEP_CURRENT:
            return f"Decision evidence: comparison {view.get('comparison_id')}."
        return ""


class Agent:
    def __init__(self, service: CaseService, tools: Tools, planner: Planner, lenders: Dict[str, Dict[str, Any]], tool_budget: int = 12):
        self.service = service
        self.tools = tools
        self.planner = planner
        self.lenders = lenders
        self.tool_budget = tool_budget
        self.turns = 0
        self.completed_cases = 0

    def cost_summary(self) -> Dict[str, Any]:
        cost = self.planner.cost_usd()
        return {"planner": self.planner.name, "total_cost_usd": cost, "turns": self.turns, "cost_per_completed_case_usd": (cost / self.completed_cases) if self.completed_cases else 0.0}

    def handle_turn(self, session: Session, case: Case, customer: Customer, message: str, intent: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        self.turns += 1
        now = self.service.now()
        if message:
            self.service.add_message(session, case, "borrower", message, {"intent": intent, "params": params})
        ctx = TurnContext(case_view=self.service.case_view(session, case), message=message, intent=intent, params=params, lenders=self.lenders)
        calls: List[Dict[str, Any]] = []
        reply = ""
        extra: Dict[str, Any] = {}
        budget = self.tool_budget
        while True:
            step = self.planner.next_step(ctx)
            if step.kind == "tool":
                if budget <= 0:
                    reply = "I reached my tool-call budget for this turn without finishing. " + ctx.case_view["next_decision"]["text"]
                    record_event(session, case, "agent.budget_exhausted", "agent", now, {"calls": [c["tool"] for c in calls]})
                    break
                budget -= 1
                result = self.tools.call(step.tool, session, case, customer, step.args)
                ctx.tool_results.append(result)
                calls.append({"tool": step.tool, "args": step.args, "ok": result.ok, "error": result.error, "authority": result.authority, "source": result.source, "retrieved_at": result.retrieved_at})
                session.flush()
                ctx.case_view = self.service.case_view(session, case)
                continue
            if step.kind == "escalate":
                reply, extra = self._escalate(session, case, customer, step, ctx)
                break
            reply = step.reply
            extra = step.data
            break
        self.service.add_message(session, case, "agent", reply, {"tool_calls": calls, "planner": self.planner.name, **{k: v for k, v in extra.items() if k == "review_screen"}})
        if case.state in st.TERMINAL_STATES:
            self.completed_cases += 1
        return {"reply": reply, "tool_calls": calls, "case_status": case.state, "case_version": case.version, "resolved_intent": ctx.resolved_intent, **extra}

    def _escalate(self, session: Session, case: Case, customer: Customer, step: PlanStep, ctx: TurnContext):
        """Borrower-authorised local decisions and operator escalations (never external writes)."""
        data = step.data
        actor = f"customer:{customer.id}"
        try:
            if data.get("decision") == "keep_current":
                self.service.decide_keep(session, case, actor)
                return step.reply, {"decision": "keep_current"}
            if data.get("manual_review"):
                transition(session, case, st.MANUAL_REVIEW, "agent-orchestrator", self.service.now(), data={"reason": data["reason"], "offer_id": data.get("offer_id"), "evidence": data.get("evidence")})
                return step.reply, {"escalated": True}
            if data.get("document_release"):
                app = next((a for a in ctx.case_view["applications"] if a["status"] == "conditions_outstanding"), None)
                if app is None:
                    return "No application is waiting for documents.", {}
                screen = self.service.draft_document_release(session, case, app["id"], data["document_release"], actor)
                return RulesPlanner()._review_reply(screen), {"review_screen": screen}
            if data.get("closing_request"):
                screen = self.service.draft_closing_request(session, case, data["closing_request"], actor)
                text = RulesPlanner()._final_review_reply(ctx.case_view, data.get("review") or {}) + "\n" + RulesPlanner()._review_reply(screen)
                return text, {"review_screen": screen}
        except WorkflowError as exc:
            return f"I could not complete that: {exc}", {}
        return step.reply, {}
