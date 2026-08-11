"""Export verified audit history to retention-locked object storage.

The exporter validates the internal hash chain, creates a Merkle commitment and
signs the export before upload.  A storage object is evidence, not the source
of truth for the live Governance audit ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime

import boto3
from platform_infra.object_storage import S3ObjectStorage

from app.container import AppContainer
from app.core.config import Settings


def _merkle_root(hashes: list[str]) -> str:
    """处理 _merkle_root 对应的当前组件内部业务步骤。"""
    if not hashes:
        return hashlib.sha256(b"").hexdigest()
    level = [bytes.fromhex(value) for value in hashes]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


async def export_tenant(settings: Settings, tenant_id: str) -> dict:
    """处理 export_tenant 对应的当前组件内部业务步骤。"""
    container = AppContainer(settings)
    await container.start()
    verification = await container.repository.verify_audit_chain(tenant_id)
    if not verification.get("valid"):
        raise RuntimeError("audit hash chain verification failed")
    cursor = 0
    events = []
    while True:
        page, next_cursor = await container.repository.list_audit_events(tenant_id, cursor, 1000)
        events.extend(item.model_dump(mode="json") for item in page)
        if next_cursor is None or len(page) < 1000:
            break
        cursor = next_cursor
    root = _merkle_root([str(item["event_hash"]) for item in events])
    exported_at = datetime.now(UTC).isoformat()
    export = {
        "tenant_id": tenant_id,
        "exported_at": exported_at,
        "event_count": len(events),
        "head_hash": verification.get("headHash", ""),
        "merkle_root": root,
        "events": events,
    }
    encoded = json.dumps(export, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    signature = (
        boto3.client("kms", region_name=settings.worm_region or None)
        .sign(
            KeyId=settings.worm_kms_key_id,
            Message=bytes.fromhex(digest),
            MessageType="DIGEST",
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )["Signature"]
        .hex()
    )
    envelope = {
        "export": export,
        "sha256": digest,
        "kms_key_id": settings.worm_kms_key_id,
        "signature_algorithm": "RSASSA_PSS_SHA_256",
        "signature": signature,
    }
    storage = S3ObjectStorage(
        bucket=settings.worm_bucket,
        prefix=settings.worm_prefix,
        endpoint_url=settings.worm_endpoint_url,
        region=settings.worm_region,
        kms_key_id="",
    )
    key, checksum = storage.put_stream(
        tenant_id,
        f"audit-{exported_at.replace(':', '-')}.json",
        io.BytesIO(json.dumps(envelope, ensure_ascii=False).encode()),
        content_type="application/json",
        retention_days=settings.worm_retention_days,
        compliance_mode=True,
    )
    return {"key": key, "sha256": checksum, "merkle_root": root}


def main() -> None:
    """处理 main 对应的当前组件内部业务步骤。"""
    import argparse

    parser = argparse.ArgumentParser(description="Export an audit chain to WORM storage")
    parser.add_argument("tenant_id")
    args = parser.parse_args()
    settings = Settings()
    if not settings.worm_bucket or not settings.worm_kms_key_id:
        raise RuntimeError("WORM bucket and KMS signing key are required")
    result = asyncio.run(export_tenant(settings, args.tenant_id))
    print(json.dumps(result, ensure_ascii=False))
