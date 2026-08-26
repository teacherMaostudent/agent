"""Export verified audit history to retention-locked object storage.

The exporter validates the internal hash chain, creates a Merkle commitment and
signs the export before upload.  A storage object is evidence, not the source
of truth for the live Governance audit ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta

import boto3
from platform_infra.object_storage import S3ObjectStorage

from app.container import AppContainer
from app.core.config import Settings


def _merkle_root(hashes: list[str]) -> str:
    """根据有序审计摘要构造 Merkle Root，用于证明导出批次未被删改或重排。"""
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


async def export_tenant(settings: Settings, tenant_id: str, repository=None) -> dict:
    """将已验证租户账本流式写入带签名、保留锁定的导出封套。

    Large event bodies spill from memory to private temporary files. Only the list of
    fixed-size event hashes stays resident for Merkle construction, so export memory is
    bounded independently of prompt/response payload size.
    """
    if repository is None:
        container = AppContainer(settings)
        await container.start()
        repository = container.repository
    verification = await repository.verify_audit_chain(tenant_id)
    if not verification.get("valid"):
        raise RuntimeError("audit hash chain verification failed")
    hashes: list[str] = []
    count = 0
    exported_at = datetime.now(UTC).isoformat()
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as events_body:
        events_body.write(b"[")
        cursor = 0
        first = True
        while True:
            page, next_cursor = await repository.list_audit_events(tenant_id, cursor, 1000)
            for item in page:
                payload = item.model_dump(mode="json")
                hashes.append(str(payload["event_hash"]))
                if not first:
                    events_body.write(b",")
                events_body.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    .encode()
                )
                first = False
                count += 1
            if next_cursor is None or len(page) < 1000:
                break
            cursor = next_cursor
        events_body.write(b"]")
        root = _merkle_root(hashes)
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as export_body:
            header = {
                "tenant_id": tenant_id,
                "exported_at": exported_at,
                "event_count": count,
                "head_hash": verification.get("headHash", ""),
                "merkle_root": root,
            }
            encoded_header = json.dumps(
                header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            export_body.write(encoded_header[:-1])
            export_body.write(b',"events":')
            events_body.seek(0)
            shutil.copyfileobj(events_body, export_body, length=1024 * 1024)
            export_body.write(b"}")
            export_body.seek(0)
            digest_builder = hashlib.sha256()
            while chunk := export_body.read(1024 * 1024):
                digest_builder.update(chunk)
            digest = digest_builder.hexdigest()
            if settings.worm_signing_mode == "hmac-local":
                if not settings.worm_local_signing_key:
                    raise RuntimeError("local WORM signing key is not configured")
                signature = hmac.new(
                    settings.worm_local_signing_key.encode(),
                    bytes.fromhex(digest),
                    hashlib.sha256,
                ).hexdigest()
                signing_key_id = "local-demo-hmac"
                signature_algorithm = "HMAC_SHA_256_DEMO_ONLY"
            elif settings.worm_signing_mode == "kms":
                if not settings.worm_kms_key_id:
                    raise RuntimeError("KMS signing key is not configured")
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
                signing_key_id = settings.worm_kms_key_id
                signature_algorithm = "RSASSA_PSS_SHA_256"
            else:
                raise RuntimeError("unsupported WORM signing mode")
            envelope_metadata = {
                "sha256": digest,
                "signing_key_id": signing_key_id,
                "signature_algorithm": signature_algorithm,
                "signature": signature,
            }
            with tempfile.SpooledTemporaryFile(
                max_size=8 * 1024 * 1024, mode="w+b"
            ) as envelope_body:
                envelope_body.write(b'{"export":')
                export_body.seek(0)
                shutil.copyfileobj(export_body, envelope_body, length=1024 * 1024)
                envelope_body.write(b",")
                encoded_metadata = json.dumps(
                    envelope_metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                envelope_body.write(encoded_metadata[1:])
                envelope_body.seek(0)
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
                    envelope_body,
                    content_type="application/json",
                    retention_days=settings.worm_retention_days,
                    compliance_mode=True,
                )
    return {
        "object_key": key,
        "sha256": checksum,
        "merkle_root": root,
        "event_count": count,
        "retention_until": (
            datetime.now(UTC) + timedelta(days=settings.worm_retention_days)
        ).isoformat(),
        "signing_key_id": signing_key_id,
        "signature_algorithm": signature_algorithm,
    }


def main() -> None:
    """执行租户审计 WORM 导出入口，并把对象存储或外部锚定失败反馈为非零退出。"""
    import argparse

    parser = argparse.ArgumentParser(description="Export an audit chain to WORM storage")
    parser.add_argument("tenant_id")
    args = parser.parse_args()
    settings = Settings()
    if not settings.worm_bucket:
        raise RuntimeError("WORM bucket is required")
    result = asyncio.run(export_tenant(settings, args.tenant_id))
    print(json.dumps(result, ensure_ascii=False))
