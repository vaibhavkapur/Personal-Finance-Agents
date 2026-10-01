"""Build an A2A registry whose insurer agents run in-process over ASGI (no ports).

Used by tests and the demo command to exercise the full A2A wire protocol without
starting separate processes. Production-like deployments use scripts/run_insurer.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from ...clock import Clock
from ...fixtures import load_insurers
from ..mock.a2a_server import A2A_PROTOCOL_VERSION, create_insurer_app
from ..mock.insurers import MockInsurer
from ..registry import AdapterRegistry
from .client import A2AInsurerAdapter


def build_inprocess_a2a_registry(clock: Clock, configs: Optional[List[Dict[str, Any]]] = None, timeout_seconds: float = 5.0) -> AdapterRegistry:
    configs = configs or load_insurers()
    insurers: Dict[str, MockInsurer] = {}
    adapters: Dict[str, A2AInsurerAdapter] = {}
    for cfg in configs:
        insurer = MockInsurer(cfg, clock, protocol="a2a/%s" % A2A_PROTOCOL_VERSION)
        base_url = "http://%s.insurer.local" % cfg["label"].lower()
        app = create_insurer_app(insurer, base_url=base_url)
        adapters[cfg["insurer_id"]] = A2AInsurerAdapter(cfg["insurer_id"], base_url, environment=cfg.get("environment", "mock"), timeout_seconds=timeout_seconds, transport=httpx.ASGITransport(app=app))
        insurers[cfg["insurer_id"]] = insurer
    # insurers are exposed so tests/demo can inject faults; a remote deployment would use /mock/faults instead.
    return AdapterRegistry(adapters, insurers, configs, "a2a")
