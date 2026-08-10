import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.rag.query_service import RagQueryService


def test_production_configuration_fails_closed() -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration"):
        Settings(deployment_environment="production")


def test_rag_acl_denies_missing_owner_by_default() -> None:
    service = RagQueryService(object(), object())

    assert service._authorized({}, "tenant-a", "user-a") is False
    assert service._authorized({"tenant_id": "tenant-a"}, "tenant-a", "user-a") is True


def test_legacy_public_access_requires_explicit_migration_switch() -> None:
    service = RagQueryService(object(), object(), allow_legacy_public_documents=True)

    assert service._authorized({}, "tenant-a", "user-a") is True
