from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.domain.errors import UnsafeEndpointError


def validate_outbound_url(
    url: str,
    allowed_hosts: list[str],
    *,
    allow_private_networks: bool,
    resolve_dns: bool = True,
) -> None:
    """校验协议、主机、端口和 DNS 解析结果，拒绝环回、私网和未注册目标以防 SSRF。

    Block SSRF by enforcing protocol, host allow-list and resolved-address policy.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeEndpointError("tool endpoint must use http or https")
    if parsed.username or parsed.password:
        raise UnsafeEndpointError("tool endpoint must not contain userinfo")
    hostname = (parsed.hostname or "").lower()
    if not hostname or not _host_allowed(hostname, allowed_hosts):
        raise UnsafeEndpointError(f"tool endpoint host is not allowlisted: {hostname or '<empty>'}")
    if not resolve_dns:
        return
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise UnsafeEndpointError(f"tool endpoint host cannot be resolved: {hostname}") from exc
    if not allow_private_networks:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise UnsafeEndpointError(
                    f"tool endpoint resolves to a non-public address: {hostname}"
                )


def _host_allowed(hostname: str, allowed_hosts: list[str]) -> bool:
    """将解析后的目标主机与精确主机/受控子域列表比较，阻止 URL 模板绕过 SSRF
    边界。

    Match exact hosts or one-label wildcard suffixes without accepting the root domain.
    """
    for allowed in allowed_hosts:
        normalized = allowed.lower()
        if normalized.startswith("*."):
            suffix = normalized[1:]
            if hostname.endswith(suffix) and hostname != normalized[2:]:
                return True
        elif hostname == normalized:
            return True
    return False
