from pathlib import Path

import pytest

from platform_infra.schema_registry import SchemaRegistry


def test_schema_registry_validates_versioned_contract() -> None:
    registry = SchemaRegistry(Path(__file__).parents[2] / "platform-contracts" / "schemas")
    registry.validate(
        "tool-catalog.v1.json",
        {
            "tools": [
                {
                    "name": "x.tool",
                    "version": "1.0.0",
                    "input_schema": {},
                    "required_permissions": [],
                    "transport": {"kind": "http"},
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="schema validation failed"):
        registry.validate("tool-catalog.v1.json", {"tools": [{"name": "bad name"}]})
