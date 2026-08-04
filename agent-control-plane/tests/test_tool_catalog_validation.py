from pathlib import Path

import pytest

from app.infrastructure.tool_catalog import ToolCatalogValidator


def test_control_plane_rejects_missing_published_tool() -> None:
    root = Path(__file__).parents[2]
    validator = ToolCatalogValidator(
        root / "tool-gateway" / "config" / "tools.json",
        root / "platform-contracts" / "schemas",
        required=True,
    )
    with pytest.raises(ValueError, match="absent"):
        validator.validate_bindings([{"tool_name": "does-not-exist", "version": "1.0.0"}])
