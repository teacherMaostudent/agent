from __future__ import annotations

import pytest

from app.domain.errors import UnsafeEndpointError
from app.infrastructure.security import validate_outbound_url


def test_url_requires_allowlisted_host() -> None:
    with pytest.raises(UnsafeEndpointError, match="not allowlisted"):
        validate_outbound_url(
            "https://attacker.example/path",
            ["api.example.com"],
            allow_private_networks=False,
            resolve_dns=False,
        )


def test_url_rejects_userinfo_and_private_resolution() -> None:
    with pytest.raises(UnsafeEndpointError, match="userinfo"):
        validate_outbound_url(
            "https://user:password@api.example.com/path",
            ["api.example.com"],
            allow_private_networks=False,
            resolve_dns=False,
        )
    with pytest.raises(UnsafeEndpointError, match="non-public"):
        validate_outbound_url(
            "http://127.0.0.1/internal",
            ["127.0.0.1"],
            allow_private_networks=False,
        )


def test_private_endpoint_requires_explicit_opt_in() -> None:
    validate_outbound_url(
        "http://127.0.0.1/internal",
        ["127.0.0.1"],
        allow_private_networks=True,
    )
