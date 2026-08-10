"""Bound untrusted text before it becomes an API response or model input."""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"), r"\1=[REDACTED]"),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "[REDACTED_EMAIL]"),
)


def redact_text(value: str) -> str:
    """Remove common credentials and direct identifiers from untrusted text."""
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def bound_untrusted(value: Any, max_chars: int = 12_000) -> Any:
    """Apply deterministic size and collection limits before prompting a model.

    Tool, retrieval and scan results are observations rather than instructions;
    limiting them here prevents a large or hostile payload from dominating a
    subsequent planning or decision prompt.
    """
    if isinstance(value, str):
        return redact_text(value[:max_chars])
    if isinstance(value, list):
        return [bound_untrusted(item, max_chars) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key): bound_untrusted(item, max_chars) for key, item in list(value.items())[:200]
        }
    return value
