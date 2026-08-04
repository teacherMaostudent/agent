import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_rejects_local_security_defaults() -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration"):
        Settings(environment="production")
