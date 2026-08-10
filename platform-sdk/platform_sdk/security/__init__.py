"""Security helpers that must behave identically at every service boundary."""

from platform_sdk.security.redaction import bound_untrusted, redact_text

__all__ = ["bound_untrusted", "redact_text"]
