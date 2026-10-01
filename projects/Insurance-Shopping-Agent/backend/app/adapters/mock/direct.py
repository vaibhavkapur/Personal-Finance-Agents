"""Direct adapter: calls the mock insurer engine in-process."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..base import AdapterCapabilities, ProviderMalformedResponse
from .insurers import MockInsurer


class DirectMockAdapter:
    def __init__(self, insurer: MockInsurer, realtime_latency: bool = False) -> None:
        self.insurer = insurer
        self.insurer_id = insurer.insurer_id
        self.realtime_latency = realtime_latency

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            environment=self.insurer.environment,
            protocol="direct",
            status_lookup_by_request_ref=True,
            supports_callbacks=False,
            supports_revised_offer_acceptance=True,
            uncertain_outcome_requires_manual_review=False,
        )

    async def _latency(self) -> None:
        if self.realtime_latency:
            await asyncio.sleep(self.insurer.behavior.get("response_latency_ms", 0) / 1000.0)

    async def request_quote(self, needs: Dict[str, Any], request_ref: str, answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        await self._latency()
        result = self.insurer.request_quote(needs, request_ref, answers)
        _validate_task_result(result)
        return result

    async def answer_question(self, task_ref: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        await self._latency()
        result = self.insurer.answer_question(task_ref, answer)
        _validate_task_result(result)
        return result

    async def get_task(self, task_ref: str) -> Dict[str, Any]:
        return self.insurer.get_task(task_ref)

    async def submit_application(self, payload: Dict[str, Any], request_ref: str) -> Dict[str, Any]:
        await self._latency()
        return self.insurer.submit_application(payload, request_ref)

    async def accept_revised_offer(self, submission_ref: str, quote_ref: str, quote_version: int, request_ref: str) -> Dict[str, Any]:
        await self._latency()
        return self.insurer.accept_revised_offer(submission_ref, quote_ref, quote_version, request_ref)

    async def get_policy_status(self, request_ref: str) -> Dict[str, Any]:
        await self._latency()
        return self.insurer.get_policy_status(request_ref)


def _validate_task_result(result: Dict[str, Any]) -> None:
    if "task_ref" not in result or "status" not in result:
        raise ProviderMalformedResponse("provider response missing task_ref/status: %s" % sorted(result.keys()))
    if result["status"] == "quoted":
        quote = result.get("quote") or {}
        if quote.get("schema") != "renters-quote/v1":
            raise ProviderMalformedResponse("quote payload does not declare renters-quote/v1")
