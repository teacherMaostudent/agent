"""Bound untrusted text before it becomes an API response or model input."""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED_EMAIL]",
    ),
)


def redact_text(value: str) -> str:
    """移除常见凭据和邮箱标识；这是提示前防线，不替代数据分类或 DLP。"""
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def bound_untrusted(value: Any, max_chars: int = 12_000) -> Any:
    """在进入模型提示词前施加确定的文本和集合上限。

    工具、检索与扫描结果都是观察数据而非指令；在此限制它们可防止大体积或恶意负载主导后续规划
    或决策提示词。
    """
    if isinstance(value, str):
        return redact_text(value[:max_chars])
    if isinstance(value, list):
        return [bound_untrusted(item, max_chars) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key): bound_untrusted(item, max_chars)
            for key, item in list(value.items())[:200]
        }
    return value
