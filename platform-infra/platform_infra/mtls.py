"""Small, shared mTLS configuration helpers for internal HTTP clients.

The helper only builds client options; certificate material remains outside the
repository (mounted secrets or a workload identity sidecar).  This keeps local
development unchanged while making production clients fail closed when mTLS is
enabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def mtls_httpx_options(
    *, enabled: bool, ca_file: str = "", cert_file: str = "", key_file: str = ""
) -> dict[str, Any]:
    """构造 httpx mTLS 参数并在启用时验证所有证书路径。

    mTLS 关闭时返回空配置以支持本地 HTTP；一旦启用，CA、客户端证书或私钥缺失都
    立即失败，禁止无证书回退。
    """
    if not enabled:
        return {}
    missing = [
        name
        for name, value in (
            ("ca_file", ca_file),
            ("cert_file", cert_file),
            ("key_file", key_file),
        )
        if not value
    ]
    if missing:
        raise ValueError("mTLS requires " + ", ".join(missing))
    paths = [Path(value) for value in (ca_file, cert_file, key_file)]
    missing_paths = [str(path) for path in paths if not path.is_file()]
    if missing_paths:
        raise ValueError("mTLS certificate file does not exist: " + ", ".join(missing_paths))
    return {"verify": ca_file, "cert": (cert_file, key_file)}
