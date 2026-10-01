from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def payload_hash(payload: Any) -> str:
    return "sha256:" + sha256_hex(canonical_json(payload))
