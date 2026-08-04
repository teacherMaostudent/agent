from pathlib import Path

from app.registry import load_registry


def test_controlled_scan_is_registered_in_tool_catalog() -> None:
    path = Path(__file__).parents[1] / "config" / "tools.json"
    registry = load_registry(path, allow_private_networks=True, max_response_bytes=1_000_000)
    spec, _ = registry.resolve("controlled_scan", "1.0.0")
    assert "file:scan" in spec.required_permissions
    assert spec.transport.kind == "http"
    assert spec.transport.url.endswith("/api/v1/query/scan")
