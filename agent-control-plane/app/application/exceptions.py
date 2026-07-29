from __future__ import annotations

from app.domain.models import ValidationReport


class ControlPlaneError(Exception):
    code = "control_plane_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(ControlPlaneError):
    code = "not_found"


class ConflictError(ControlPlaneError):
    code = "conflict"


class PolicyViolationError(ControlPlaneError):
    code = "policy_violation"


class InvalidStateError(ControlPlaneError):
    code = "invalid_state"


class DraftValidationError(ControlPlaneError):
    code = "draft_validation_failed"

    def __init__(self, report: ValidationReport) -> None:
        super().__init__("Agent draft contains validation errors.")
        self.report = report
        self.details = {"report": report.model_dump(mode="json")}
