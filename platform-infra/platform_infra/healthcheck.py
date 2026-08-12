"""通用内部服务健康探针：与 ASGI 监听模式一致地支持 HTTP 与双向 TLS。"""

from __future__ import annotations

import os
import ssl
import sys
from urllib.request import urlopen


def _ssl_context() -> ssl.SSLContext | None:
    """在服务端启用 mTLS 时加载同一工作负载证书，健康检查不能成为无证书旁路。"""
    if os.environ.get("PLATFORM_MTLS_SERVER_ENABLED", "false").lower() != "true":
        return None
    ca_file = os.environ.get("PLATFORM_MTLS_CA_FILE", "")
    cert_file = os.environ.get("PLATFORM_MTLS_CERT_FILE", "")
    key_file = os.environ.get("PLATFORM_MTLS_KEY_FILE", "")
    if not all((ca_file, cert_file, key_file)):
        raise ValueError("mTLS healthcheck requires CA, certificate and key paths")
    context = ssl.create_default_context(cafile=ca_file)
    # 容器探针连接 127.0.0.1; 服务证书的 SAN 属于服务 DNS, 而不是 loopback 地址。
    context.check_hostname = False
    context.load_cert_chain(cert_file, key_file)
    return context


def main() -> int:
    """探测本地 ready 端点，失败返回非零状态供容器编排器摘除异常实例。"""
    url = os.environ.get("PLATFORM_HEALTHCHECK_URL", "http://127.0.0.1:8000/health/ready")
    if _ssl_context() is not None and url.startswith("http://"):
        url = "https://" + url.removeprefix("http://")
    try:
        with urlopen(url, timeout=2, context=_ssl_context()) as response:
            return 0 if 200 <= response.status < 400 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
