from __future__ import annotations

from typing import Any


class GovernanceError(Exception):
    code = "governance_error"

    def __init__(self, message: str, **details: Any) -> None:
        """保存治理错误的稳定代码、对外消息及可序列化细节。"""
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(GovernanceError):
    code = "not_found"


class InvalidStateError(GovernanceError):
    code = "invalid_state"
