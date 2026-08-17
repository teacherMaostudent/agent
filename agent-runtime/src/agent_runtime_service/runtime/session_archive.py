"""Session Event Ledger 的对象存储归档，在线数据库只保留定位与完整性摘要。"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from platform_infra.object_storage import S3ObjectStorage


class SessionArchiveService:
    """将完整会话账本写入受保留策略约束的对象存储，不参与在线执行状态决策。"""

    def __init__(
        self,
        storage: S3ObjectStorage,
        *,
        retention_days: int,
        compliance_mode: bool,
    ) -> None:
        """注入部署侧配置的对象存储与保留策略；业务请求不得覆盖这些参数。"""
        self._storage = storage
        self._retention_days = retention_days
        self._compliance_mode = compliance_mode

    def archive(self, tenant_id: str, session_id: str, payload: dict[str, Any]) -> tuple[str, str]:
        """写入确定性 JSON 归档并返回对象键与 SHA-256，方便审计完整性验证。"""
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._storage.put_stream(
            namespace=f"session-ledger/{tenant_id}",
            filename=f"{session_id}.json",
            stream=BytesIO(encoded),
            content_type="application/json",
            retention_days=self._retention_days,
            compliance_mode=self._compliance_mode,
        )
