"""Policy providers behind one structured tool-calling interface.

* ``RulesPolicy`` is a deterministic policy that drives the maturity journey
  from case state and simple parsing of the customer's message. It is the
  "rules-only workflow" baseline in the evaluation and the default when no
  model credentials are configured.
* ``OpenAICompatiblePolicy`` calls any OpenAI-compatible chat-completions
  endpoint with the same tool schemas.

Both return either a ``ToolCall`` or a ``FinalAnswer``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import settings
from app.domain.money import format_minor


@dataclass
class ToolCall:
    name: str
    arguments: dict


@dataclass
class FinalAnswer:
    text: str
    questions: list[str] = field(default_factory=list)
    refused: bool = False
    escalated: bool = False


@dataclass
class ToolResult:
    name: str
    arguments: dict
    result: dict


@dataclass
class Turn:
    customer_id: str
    user_message: str
    case: dict | None
    tool_results: list[ToolResult] = field(default_factory=list)

    def last(self, name: str) -> ToolResult | None:
        for tr in reversed(self.tool_results):
            if tr.name == name:
                return tr
        return None

    def has(self, name: str) -> bool:
        return self.last(name) is not None


class Policy(Protocol):
    model_version: str

    async def decide(self, turn: Turn, tool_schemas: list[dict]) -> ToolCall | FinalAnswer: ...


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(k\b)?", re.I)
_DAYS = re.compile(r"(\d+)\s*(day|days)\b", re.I)
_MONTHS = re.compile(r"(\d+)\s*[- ]?(month|months|mo)\b", re.I)
_YEARS = re.compile(r"(\d+)\s*[- ]?(year|years|yr)\b", re.I)


def parse_amounts_minor(text: str) -> list[int]:
    from decimal import Decimal

    out = []
    for number, k in _AMOUNT.findall(text):
        value = Decimal(number.replace(",", ""))
        if k:
            value *= 1000
        out.append(int((value * 100).quantize(Decimal("1"))))
    return out


def parse_lockup_days(text: str) -> int | None:
    t = text.lower()
    if any(p in t for p in ("no lock", "not lock", "no lockup", "no lock-up", "zero days")) or re.search(r"\bliquid\b", t):
        return 0
    m = _DAYS.search(t)
    if m:
        return int(m.group(1))
    m = _MONTHS.search(t)
    if m:
        return int(m.group(1)) * 30
    m = _YEARS.search(t)
    if m:
        return int(m.group(1)) * 365
    if re.search(r"\b(a|one) year\b", t):
        return 365
    if re.search(r"\bsix months\b|\bhalf a year\b", t):
        return 180
    return None


def parse_yes_no(text: str) -> bool | None:
    t = text.lower()
    if re.search(r"\b(yes|yeah|yep|correct|it does|includes|included|already includes|that includes)\b", t):
        return True
    if re.search(r"\b(no|nope|doesn't|does not|separate|on top|in addition|excludes|not included)\b", t):
        return False
    return None


def parse_choice(text: str, options: list[dict]) -> dict | None:
    """Map the customer's words to one offer in the comparison list."""
    t = text.lower()
    comparable = [o for o in options if o.get("comparable")]
    m = re.search(r"option\s*(\d)", t)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(comparable):
            return comparable[idx]
    for o in options:
        offer = o["offer"]
        name = offer["product_name"].lower()
        provider = offer["provider_id"].replace("bank_", "").lower()
        if provider in t and (offer["offer_kind"] != "cd_renewal" or "renew" in t or "cd" in t or "month" in t):
            # Provider named: disambiguate by term words if there are several products.
            same_provider = [x for x in options if x["offer"]["provider_id"] == offer["provider_id"]]
            if len(same_provider) == 1:
                return o
        if offer["term_days"]:
            months = round(offer["term_days"] / 30.4)
            if re.search(rf"\b{months}[- ]?month", t) or (months == 12 and re.search(r"\b(12[- ]?month|one[- ]year|1[- ]year|twelve[- ]month)\b", t)) or (months == 6 and re.search(r"\b(six[- ]month|6[- ]month)\b", t)) or (months == 9 and re.search(r"\b(nine[- ]month|9[- ]month)\b", t)):
                return o
        if offer["offer_kind"] == "savings_transfer" and re.search(r"\b(savings|hysa|high[- ]yield|transfer|liquid)\b", t):
            return o
        if name in t:
            return o
    if re.search(r"\b(renew)\b", t) and not re.search(r"\b(don't|do not|not)\s+renew", t):
        renewals = [o for o in comparable if o["offer"]["offer_kind"] == "cd_renewal"]
        if len(renewals) == 1:
            return renewals[0]
    return None


def wants_approval(text: str) -> bool:
    return bool(re.search(r"\b(approve|approved|go ahead and (submit|execute|do it)|submit it|execute it|just do it|confirm the transfer)\b", text.lower()))


def cite(source: dict) -> str:
    return f"[{source.get('provider_id')} v{source.get('product_version')} retrieved {source.get('retrieved_at')} ({source.get('environment')})]"


# --------------------------------------------------------------------------- #
# Rules policy
# --------------------------------------------------------------------------- #


class RulesPolicy:
    model_version = "rules-v1"

    async def decide(self, turn: Turn, tool_schemas: list[dict]) -> ToolCall | FinalAnswer:
        msg = turn.user_message
        # Escalate on repeated tool errors with the evidence.
        errors = [tr for tr in turn.tool_results if isinstance(tr.result, dict) and tr.result.get("error")]
        if errors:
            last = errors[-1]
            if last.result.get("error") in ("refused",):
                return FinalAnswer(f"I can't do that: {last.result.get('message')}.", refused=True)
            if len(errors) >= 2 or last.name in ("prepare_bank_instruction", "open_maturity_case", "record_customer_answers"):
                return self._explain_error(turn, last)

        if turn.case is None:
            return self._no_case(turn, msg)

        prepared = turn.last("prepare_bank_instruction")
        if prepared is not None and not prepared.result.get("error"):
            return self._review_summary(prepared.result)

        state = turn.case["state"]
        if wants_approval(msg) and state in ("awaiting_approval", "evaluating", "needs_information", "collecting"):
            if state == "awaiting_approval":
                return FinalAnswer(
                    "I can't approve or submit anything on your behalf. The proposed instruction is on your review screen with the exact amount, destination, terms and irreversible effect; approving there binds your approval to that exact payload and the executor will submit it once.",
                    refused=True,
                )
        if state == "needs_requote" and not re.search(r"show|revis|re-?evaluate|compare|again|option|offers|proceed|continue|what can i do", msg.lower()):
            # Explain the invalidated approval first; re-evaluate only when asked.
            return self._status(turn)
        if state in ("collecting", "needs_information", "needs_requote"):
            return self._collect(turn, msg)
        if state == "evaluating":
            return self._evaluating(turn, msg)
        if state == "awaiting_approval":
            return self._awaiting(turn)
        return self._status(turn)

    # -- helpers -----------------------------------------------------------
    def _no_case(self, turn: Turn, msg: str) -> ToolCall | FinalAnswer:
        snap = turn.last("read_cash_snapshot")
        if snap is None:
            return ToolCall("read_cash_snapshot", {})
        deposits = snap.result.get("deposits", [])
        if not deposits:
            return FinalAnswer("I don't see a fixed-term deposit on your verified accounts, so there is nothing maturing to plan for.")
        deposit = deposits[0]
        amounts = [a for a in parse_amounts_minor(msg) if a != deposit["principal_minor"]]
        if not amounts:
            return FinalAnswer(
                f"Your {deposit['id']} ({format_minor(deposit['principal_minor'])}) matures on {deposit['maturity_date']}. How much cash do you want to keep available after maturity for upcoming expenses?",
                questions=["minimum_buffer_minor"],
            )
        reserve = amounts[0]
        post = [o["id"] for o in snap.result.get("obligations_on_record", []) if o["certainty"] == "confirmed" and o["due_date"] >= deposit["maturity_date"]]
        args: dict[str, Any] = {"deposit_id": deposit["id"], "minimum_buffer_minor": reserve, "obligation_ids": post}
        lock = parse_lockup_days(msg)
        if lock is not None:
            args["preferred_lockup_days"] = lock
        return ToolCall("open_maturity_case", args)

    def _collect(self, turn: Turn, msg: str) -> ToolCall | FinalAnswer:
        case = turn.case
        missing = case.get("missing_fields") or []
        if missing and not turn.has("record_customer_answers"):
            answers: dict[str, Any] = {"case_id": case["id"]}
            if "buffer_includes_obligations" in missing:
                yn = parse_yes_no(msg)
                if yn is not None:
                    answers["buffer_includes_obligations"] = yn
            if "preferred_lockup_days" in missing:
                lock = parse_lockup_days(msg)
                if lock is not None:
                    answers["preferred_lockup_days"] = lock
            if len(answers) > 1:
                return ToolCall("record_customer_answers", answers)
        if not turn.has("evaluate_case"):
            return ToolCall("evaluate_case", {"case_id": case["id"]})
        ev = turn.last("evaluate_case").result
        if case["state"] == "needs_information" or (ev.get("case") or {}).get("missing_fields"):
            return self._ask_questions(turn, ev)
        return self._present_options(turn, ev)

    def _evaluating(self, turn: Turn, msg: str) -> ToolCall | FinalAnswer:
        case = turn.case
        ev = turn.last("evaluate_case")
        if ev is None:
            return ToolCall("evaluate_case", {"case_id": case["id"]})
        options = ev.result.get("options", [])
        prepared = turn.last("prepare_bank_instruction")
        if prepared is not None:
            return self._review_summary(prepared.result)
        # Never prepare an action in the same turn the case was opened or answers
        # were recorded: the customer sees the comparison first and then chooses.
        choice = None if (turn.has("open_maturity_case") or turn.has("record_customer_answers")) else parse_choice(msg, options)
        if choice is None:
            return self._present_options(turn, ev.result)
        if not choice["comparable"]:
            offer = choice["offer"]
            return FinalAnswer(
                f"{offer['product_name']} from {offer['provider_id']} {cite(choice['source'])} can't be selected: {', '.join(choice['exclusion_reasons'])}. "
                f"Its {offer['apy_decimal']} APY is the highest advertised, but eligibility is {offer['eligibility_status']} ({offer['eligibility_notes'] or 'no notes'}), so it is not comparable. "
                "Would you like one of the feasible options instead?"
            )
        # Pass the customer's stated amount as-is; the engine rejects breaches with evidence.
        amounts = parse_amounts_minor(msg)
        args = {"case_id": case["id"], "plan_id": ev.result.get("plan_id"), "option_id": choice["offer_id"]}
        if amounts:
            args["amount_minor"] = amounts[0]
        return ToolCall("prepare_bank_instruction", args)

    def _awaiting(self, turn: Turn) -> ToolCall | FinalAnswer:
        status = turn.last("get_instruction_status")
        if status is None:
            return ToolCall("get_instruction_status", {"case_id": turn.case["id"]})
        return FinalAnswer(
            "Your proposed instruction is waiting for your approval on the review screen. Nothing has been sent to the bank. "
            "If the offer terms change before you approve, the proposal is invalidated and re-quoted."
        )

    def _status(self, turn: Turn) -> ToolCall | FinalAnswer:
        status = turn.last("get_instruction_status")
        if status is None:
            return ToolCall("get_instruction_status", {"case_id": turn.case["id"]})
        r = status.result
        state = r.get("state")
        instr = r.get("instruction") or {}
        action = r.get("action") or {}
        ref = action.get("provider_reference") or instr.get("external_ref")
        if state == "completed":
            recon = instr.get("reconciliation") or {}
            return FinalAnswer(
                f"Done and verified. The bank recorded your {instr.get('instruction_type', 'instruction')} of {format_minor(instr['amount_minor'])} effective {instr['effective_at']} under reference {ref}; "
                f"{len([c for c in recon.get('checks', []) if c['ok']])} of {len(recon.get('checks', []))} reconciliation checks matched (evidence: {r.get('completion_evidence_ref')})."
            )
        if state == "submitted":
            return FinalAnswer(f"The bank accepted the instruction (reference {ref}) for {format_minor(instr['amount_minor'])} effective {instr['effective_at']}. It stays 'submitted' until the bank records the result; I'll verify the balances then.")
        if state == "outcome_unknown":
            return FinalAnswer(f"The bank connection dropped after we submitted request {instr.get('request_ref')}. The outcome is unknown until the worker looks it up by that original reference; no second instruction will be created.", escalated=True)
        if state == "verifying":
            return FinalAnswer(f"The bank reports the instruction (reference {ref}) as effective; balances are being reconciled against the approved amount before the case closes.")
        if state == "rejected":
            return FinalAnswer(f"The bank declined the instruction: {r.get('review_reason')}. No funds moved. We can re-evaluate with the remaining options.", escalated=True)
        if state == "manual_review":
            return FinalAnswer(f"The case is held for review: {r.get('review_reason')}. It is not completed. An operator has the provider references and evidence.", escalated=True)
        if state == "needs_requote":
            return FinalAnswer(f"Your approval was invalidated because the terms changed: {r.get('review_reason')}. Let me re-evaluate the current offers so you can approve again if you wish.", questions=["re-evaluate"])
        if state == "approved":
            return FinalAnswer("Your approval is recorded and bound to the exact instruction. The executor will re-check balances and offer terms, then submit it once.")
        if state == "cancelled":
            return FinalAnswer("This case was cancelled; nothing was sent to the bank.")
        return FinalAnswer(f"The case is currently {state}.")

    def _ask_questions(self, turn: Turn, ev: dict) -> FinalAnswer:
        case = ev.get("case") or turn.case
        qs = case.get("outstanding_questions") or []
        lines = []
        lines.append(f"Before I compare options I need {len(qs)} thing{'s' if len(qs) != 1 else ''} the records can't tell me:")
        for q in qs:
            lines.append(f"- {q['question']}")
        warnings = ev.get("warnings") or case.get("warnings") or []
        for w in warnings:
            lines.append(f"Note: {w}")
        return FinalAnswer("\n".join(lines), questions=[q["field"] for q in qs])

    def _present_options(self, turn: Turn, ev: dict) -> FinalAnswer:
        dep = ev.get("deposit", {})
        lines = [
            f"Plan for {dep.get('id')} maturing {dep.get('maturity_date')}: principal {format_minor(dep.get('principal_minor', 0))}, "
            f"of which {format_minor(ev.get('reserved_minor', 0))} stays available (buffer {format_minor(ev.get('effective_buffer_minor', 0))} plus dated bills) and up to {format_minor(ev.get('max_lockable_minor', 0))} can be placed."
        ]
        for w in ev.get("warnings") or []:
            lines.append(f"Warning: {w}")
        comparable = [o for o in ev.get("options", []) if o["comparable"]]
        excluded = [o for o in ev.get("options", []) if not o["comparable"]]
        lines.append(f"Feasible options over a common {ev.get('comparison_horizon_days')}-day horizon on {format_minor(ev.get('max_lockable_minor', 0))}:")
        for i, o in enumerate(comparable, 1):
            offer = o["offer"]
            lock = f"locked until {o['locked_until']}" if o["locked_until"] else "liquid"
            lines.append(
                f"{i}. {offer['product_name']} ({offer['provider_id']}) {offer['apy_decimal']} APY {offer['rate_type']}, {lock}, fees {format_minor(o['fees_minor'])}: "
                f"about {format_minor(o['net_at_horizon_minor'])} net at horizon {cite(o['source'])}."
                + (f" {o['early_withdrawal_note']}" if o.get("early_withdrawal_note") else "")
                + (" " + " ".join(o["warnings"]) if o.get("warnings") else "")
            )
        for o in excluded:
            offer = o["offer"]
            lines.append(f"Not comparable: {offer['product_name']} ({offer['provider_id']}) {offer['apy_decimal']} APY: {', '.join(o['exclusion_reasons'])} {cite(o['source'])}.")
        lines.append("Which option should I prepare for your approval? You can also name a smaller amount.")
        return FinalAnswer("\n".join(lines), questions=["option_choice"])

    def _review_summary(self, review: dict) -> FinalAnswer:
        instr = review.get("instruction", {})
        offer = review.get("offer") or {}
        dest = review.get("destination_account") or {}
        liq = review.get("liquidity") or {}
        return FinalAnswer(
            f"I've prepared a {instr.get('instruction_type', '').replace('_', ' ')} of {review.get('amount_display')} effective {instr.get('effective_at')} into {dest.get('display_name', instr.get('destination_account_id'))} "
            f"at {offer.get('apy_decimal')} APY (product version {offer.get('product_version')}, retrieved {offer.get('source', {}).get('retrieved_at')}). "
            f"Lowest projected available cash after the allocation: {format_minor(liq.get('lowest_balance_after_allocation_minor', 0))} against a buffer of {format_minor(liq.get('buffer_minor', 0))}. "
            f"Irreversible effect: {review.get('irreversible_effect')} "
            f"Nothing is sent until you approve it on the review screen (action {review.get('action_id')}, payload hash {review.get('action_payload_hash')})."
        )

    def _explain_error(self, turn: Turn, tr: ToolResult) -> FinalAnswer:
        err = tr.result
        code = err.get("error")
        if code == "liquidity_violation":
            d = err.get("details", {})
            return FinalAnswer(f"That amount breaches your cash buffer: {err.get('message')}. I can prepare up to {format_minor(d.get('max_lockable_minor', 0))} instead.", questions=["amount"])
        if code == "case_already_open":
            return FinalAnswer("There is already an open case for this deposit; I'll continue with it. What would you like to do next?")
        if code == "access_revoked":
            return FinalAnswer(f"Access to the deposit account has been revoked, so I can't read balances or prepare an instruction: {err.get('message')}", escalated=True)
        return FinalAnswer(f"I couldn't complete that step ({code}): {err.get('message')}. Evidence: tool {tr.name} with {json.dumps(err.get('details', {}))}.", escalated=True)


# --------------------------------------------------------------------------- #
# OpenAI-compatible policy
# --------------------------------------------------------------------------- #


class OpenAICompatiblePolicy:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.model_version = model
        self._messages: list[dict] = []

    async def decide(self, turn: Turn, tool_schemas: list[dict]) -> ToolCall | FinalAnswer:
        from app.agent.prompts import SYSTEM_PROMPT

        if not self._messages:
            context = {"case": turn.case, "customer_id": turn.customer_id}
            self._messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": "Current case record (authoritative): " + json.dumps(context, default=str)},
                {"role": "user", "content": turn.user_message},
            ]
        # Append tool results not yet in the transcript.
        while len([m for m in self._messages if m["role"] == "tool"]) < len(turn.tool_results):
            tr = turn.tool_results[len([m for m in self._messages if m["role"] == "tool"])]
            call_id = f"call_{len(self._messages)}"
            self._messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tr.name, "arguments": json.dumps(tr.arguments)}}]})
            self._messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(tr.result, default=str)[:20000]})
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": self._messages, "tools": tool_schemas, "tool_choice": "auto", "temperature": 0},
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]["message"]
        calls = choice.get("tool_calls") or []
        if calls:
            fn = calls[0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            return ToolCall(fn["name"], args)
        text = choice.get("content") or ""
        return FinalAnswer(text, questions=["?"] if text.strip().endswith("?") else [])


def default_policy() -> Policy:
    if settings.llm_base_url and settings.llm_api_key:
        return OpenAICompatiblePolicy(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    return RulesPolicy()
