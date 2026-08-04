from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SECRET_KEYS = re.compile(
    r"(?:authorization|api[-_]?key|password|secret|access[-_]?token|refresh[-_]?token)",
    re.IGNORECASE,
)
_CONTENT_KEYS = {
    "messages",
    "content",
    "prompt",
    "input",
    "output",
    "request",
    "response",
    "arguments",
    "tool_arguments",
}
_EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]{1,64})@([\w.-]+\.[A-Za-z]{2,})")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


def protect_payload(value: Any, *, capture_content: bool) -> Any:
    if isinstance(value, dict):
        protected: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                protected[key] = "[REDACTED_SECRET]"
            elif not capture_content and str(key).lower() in _CONTENT_KEYS:
                protected[key] = _content_reference(item)
            else:
                protected[key] = protect_payload(item, capture_content=capture_content)
        return protected
    if isinstance(value, list):
        return [protect_payload(item, capture_content=capture_content) for item in value]
    if isinstance(value, str):
        return _PHONE.sub("[REDACTED_PHONE]", _EMAIL.sub(r"***@\2", value))
    return value


def classify_payload(*, capture_content: bool, protected: Any) -> str:
    if capture_content:
        return "restricted"
    serialized = json.dumps(protected, ensure_ascii=False)
    return "confidential" if "REDACTED" in serialized or "sha256" in serialized else "internal"


def sampled(identifier: str, rate: float) -> bool:
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    bucket = int(hashlib.sha256(identifier.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def _content_reference(value: Any) -> dict[str, Any]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "redacted": True,
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "characters": len(serialized),
    }
