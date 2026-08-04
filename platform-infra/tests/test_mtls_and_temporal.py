import pytest

from platform_infra.mtls import mtls_httpx_options


def test_mtls_requires_material_when_enabled() -> None:
    with pytest.raises(ValueError, match="mTLS requires"):
        mtls_httpx_options(enabled=True)
