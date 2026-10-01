"""Loader for the versioned synthetic fixtures kept in the repository."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from .config import FIXTURES_DIR

INSURER_FILES = ("northwind_a.json", "harborline_b.json", "cedar_c.json")


def _load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def load_state_profile() -> Dict[str, Any]:
    return _load(FIXTURES_DIR / "state_profile_ca.json")


@lru_cache(maxsize=None)
def load_households() -> Dict[str, Any]:
    return _load(FIXTURES_DIR / "households.json")


@lru_cache(maxsize=None)
def load_insurers() -> List[Dict[str, Any]]:
    return [_load(FIXTURES_DIR / "insurers" / name) for name in INSURER_FILES]


def insurer_by_id(insurer_id: str) -> Dict[str, Any]:
    for insurer in load_insurers():
        if insurer["insurer_id"] == insurer_id:
            return insurer
    raise KeyError(insurer_id)


def household_by_customer(customer_id: str) -> Dict[str, Any]:
    for household in load_households()["households"]:
        if household["customer_id"] == customer_id:
            return household
    raise KeyError(customer_id)


def customer_tokens() -> Dict[str, str]:
    return {h["auth_token"]: h["customer_id"] for h in load_households()["households"]}


def load_eval_cases() -> List[Dict[str, Any]]:
    return _load(FIXTURES_DIR / "eval_cases.json")["cases"]
