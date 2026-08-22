"""Web 与 Desktop Connector 配对的共享契约；不包含 Runtime 或供应商长期凭据。"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ConnectorStatus(StrEnum):
    """设备配对生命周期；撤销和过期均不能恢复为已连接。"""

    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    REVOKED = "REVOKED"


class ConnectorPairingRequest(BaseModel):
    """浏览器发起一次短时配对请求；能力只声明，不直接授予服务端工具权限。"""

    device_name: str = Field(min_length=1, max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=30)


class ConnectorPairing(BaseModel):
    """服务端保存的配对记录；pairing_code 只在创建响应中出现一次。"""

    connector_id: str
    tenant_id: str
    user_id: str
    device_name: str
    capabilities: list[str]
    status: ConnectorStatus
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConnectorPairingCreated(ConnectorPairing):
    """仅创建响应携带明文配对码；持久化层只保存其哈希。"""

    pairing_code: str


class ConnectorGrantRequest(BaseModel):
    """把已配对设备绑定到某次 Run 的一个具体工具版本。"""

    connector_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    tool_version: str = Field(min_length=1, max_length=100)
    expires_in_seconds: int = Field(default=60, ge=5, le=300)


class ConnectorTaskRequest(BaseModel):
    """Runtime 投递给已配对桌面设备的无副作用工作声明。"""

    run_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    tool_version: str = Field(min_length=1, max_length=100)
    arguments: dict[str, object] = Field(default_factory=dict)
    expires_in_seconds: int = Field(default=300, ge=30, le=3_600)
