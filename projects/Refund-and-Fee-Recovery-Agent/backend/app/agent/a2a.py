"""Optional A2A-style boundary to a simulated merchant support agent.

A2A belongs between independently deployed agents. This gateway shows the
shape of that boundary without claiming interoperability with any live
system: it pins a protocol version, agrees on a domain payload schema, maps
external task IDs to internal case IDs, and delegates the actual side effect
to the provider adapter. It grants no authority: the packet it sends is the
already-approved action payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..adapters.base import RecoveryProviderAdapter
from ..ids import new_id

A2A_PROTOCOL_VERSION = "a2a/0.3-fixture"
PAYLOAD_SCHEMA = "recovery.merchant_followup.v1"


@dataclass
class TaskMapping:
    external_task_id: str
    case_id: str
    action_id: str
    request_ref: str
    state: str = "submitted"
    messages: list = field(default_factory=list)


class MerchantAgentGateway:
    def __init__(self, adapter: RecoveryProviderAdapter) -> None:
        self.adapter = adapter
        self._by_task: Dict[str, TaskMapping] = {}
        self._by_case: Dict[str, str] = {}

    def agent_card(self) -> Dict[str, Any]:
        return {
            "name": "mock-merchant-support-agent",
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "skills": [{"id": "refund_followup", "inputSchema": PAYLOAD_SCHEMA}],
            "environment": self.adapter.capabilities().environment,
        }

    async def send_task(self, *, case_id: str, action_id: str, request_ref: str, approved_payload: Dict[str, Any]) -> TaskMapping:
        if case_id in self._by_case:
            return self._by_task[self._by_case[case_id]]
        if approved_payload.get("type") not in ("send_merchant_message",):
            raise ValueError("gateway only carries approved merchant follow-up payloads")
        message = {"protocolVersion": A2A_PROTOCOL_VERSION, "schema": PAYLOAD_SCHEMA, "parts": [{"kind": "data", "data": {k: approved_payload[k] for k in ("order_ref", "reason_code", "subject", "body", "amount_minor", "currency", "attachments_hash")}}]}
        result = await self.adapter.open_case(dict(approved_payload, a2a_message=message), request_ref)
        mapping = TaskMapping(external_task_id=result.data.get("case_ref") or new_id("a2a_task"), case_id=case_id, action_id=action_id, request_ref=request_ref, state=result.data.get("status", "submitted"))
        mapping.messages.append(message)
        self._by_task[mapping.external_task_id] = mapping
        self._by_case[case_id] = mapping.external_task_id
        return mapping

    def resolve(self, external_task_id: str) -> Optional[TaskMapping]:
        return self._by_task.get(external_task_id)

    def task_for_case(self, case_id: str) -> Optional[TaskMapping]:
        ext = self._by_case.get(case_id)
        return self._by_task.get(ext) if ext else None
