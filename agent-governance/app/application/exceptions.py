from __future__ import annotations

from typing import Any


class GovernanceError(Exception):
    code = "governance_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(GovernanceError):
    code = "not_found"


class InvalidStateError(GovernanceError):
    code = "invalid_state"
