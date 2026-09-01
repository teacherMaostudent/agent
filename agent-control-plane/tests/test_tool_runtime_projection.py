"""Control Plane 到 Tool Gateway 的只读 Catalog 投影契约。"""

from pathlib import Path

import pytest

from app.domain.models import ToolVersion, utc_now
from app.infrastructure.tool_runtime_projection import ToolRuntimeProjectionPublisher


class _Repository:
    """最小发布态仓储替身，证明投影不会再从静态 JSON 读取工具定义。"""

    async def list_published_tool_versions(self) -> list[ToolVersion]:
        now = utc_now()
        return [
            ToolVersion(
                tenant_id="tenant-a",
                version_id="tv_scan",
                tool_id="controlled_scan",
                semantic_version="1.0.0",
                source_revision=1,
                content_sha256="a" * 64,
                runtime_definition={
                    "name": "controlled_scan",
                    "version": "1.0.0",
                    "input_schema": {},
                    "transport": {"kind": "http"},
                    "required_permissions": ["file:scan"],
                },
                status="published",
                change_summary="seed",
                published_by="test",
                published_at=now,
                updated_at=now,
            )
        ]


@pytest.mark.anyio
async def test_published_catalog_becomes_a_hash_pinned_runtime_projection() -> None:
    """Gateway 接收的投影只包含运行定义，且摘要能证明其内容没有漂移。"""
    root = Path(__file__).parents[2]
    publisher = ToolRuntimeProjectionPublisher(
        _Repository(), root / "platform-contracts" / "schemas"
    )

    projection = await publisher.current()

    assert projection["projection_version"] == "tool-runtime-projection/v1"
    assert projection["catalog_release_id"].startswith("tool-catalog-")
    assert len(projection["content_sha256"]) == 64
    assert {item["name"] for item in projection["catalog"]["tools"]} == {
        "controlled_scan",
    }
