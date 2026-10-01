"""Builds the adapter set for the configured mode (direct in-process or A2A over HTTP)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..clock import Clock
from ..config import Settings
from ..fixtures import load_insurers
from .a2a.client import A2AInsurerAdapter
from .base import InsurerQuoteAdapter
from .mock.direct import DirectMockAdapter
from .mock.insurers import MockInsurer, build_mock_insurers


class AdapterRegistry:
    def __init__(self, adapters: Dict[str, InsurerQuoteAdapter], insurers: Dict[str, MockInsurer], configs: List[Dict[str, Any]], mode: str) -> None:
        self.adapters = adapters
        self.insurers = insurers  # only populated in direct mode (or tests)
        self.configs = {c["insurer_id"]: c for c in configs}
        self.mode = mode

    def get(self, insurer_id: str) -> InsurerQuoteAdapter:
        return self.adapters[insurer_id]

    def ids(self) -> List[str]:
        return list(self.adapters.keys())

    def config(self, insurer_id: str) -> Dict[str, Any]:
        return self.configs[insurer_id]

    def questions(self, insurer_id: str) -> List[Dict[str, Any]]:
        return self.configs[insurer_id]["questions"]

    def question(self, insurer_id: str, question_id: str) -> Optional[Dict[str, Any]]:
        for q in self.questions(insurer_id):
            if q["id"] == question_id:
                return q
        return None

    def find_question_owner(self, question_id: str) -> Optional[str]:
        for insurer_id, cfg in self.configs.items():
            if any(q["id"] == question_id for q in cfg["questions"]):
                return insurer_id
        return None

    def capability_matrix(self) -> List[Dict[str, Any]]:
        rows = []
        for insurer_id, adapter in self.adapters.items():
            caps = adapter.capabilities().to_dict()
            caps["insurer_id"] = insurer_id
            caps["display_name"] = self.configs[insurer_id]["display_name"]
            rows.append(caps)
        return rows


def build_registry(settings: Settings, clock: Clock, configs: Optional[List[Dict[str, Any]]] = None) -> AdapterRegistry:
    configs = configs or load_insurers()
    if settings.adapter_mode == "a2a":
        adapters: Dict[str, InsurerQuoteAdapter] = {
            cfg["insurer_id"]: A2AInsurerAdapter(
                cfg["insurer_id"], settings.a2a_insurer_urls[cfg["insurer_id"]], environment=cfg.get("environment", "mock"),
                timeout_seconds=settings.provider_timeout_seconds,
            )
            for cfg in configs
        }
        return AdapterRegistry(adapters, {}, configs, "a2a")
    insurers = build_mock_insurers(configs, clock, protocol="direct")
    adapters = {iid: DirectMockAdapter(ins) for iid, ins in insurers.items()}
    return AdapterRegistry(adapters, insurers, configs, "direct")
