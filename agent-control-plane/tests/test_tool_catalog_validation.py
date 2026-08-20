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


def test_control_plane_freezes_tool_catalog_governance_facts() -> None:
    """发布绑定中的权限与风险来自 Tool Catalog，不能由 Agent Draft 自行降级。"""
    root = Path(__file__).parents[2]
    validator = ToolCatalogValidator(
        root / "tool-gateway" / "config" / "tools.json",
        root / "platform-contracts" / "schemas",
        required=True,
    )

    resolved = validator.resolve_bindings(
        [
            {
                "tool_name": "controlled_scan",
                "version": "1.0.0",
                "risk": "write_high_risk",
                "approval_required": True,
                "idempotent": False,
            }
        ]
    )[0]

    assert resolved["risk"] == "read_only"
    assert resolved["required_permissions"] == ["file:scan"]
    assert resolved["approval_required"] is False
    assert resolved["idempotent"] is True
