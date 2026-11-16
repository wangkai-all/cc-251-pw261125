"""Small shared helpers: ISO timestamps, JSON and stable hashing."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(obj) -> str:
    """Canonical JSON for storage and dedup keys."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str):
    return json.loads(text)


def require_date(value: str, what: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{what} must be YYYY-MM-DD, got {value!r}") from exc
    return value


def require_ts(value: str, what: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{what} must be YYYY-MM-DDTHH:MM:SSZ, got {value!r}") from exc
    return value
