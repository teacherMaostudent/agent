"""Shared S3-compatible object-storage primitives for durable platform artifacts."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import boto3


class S3ObjectStorage:
    """Write tenant-prefixed objects with optional retention-lock semantics."""
    """Content-addressed S3 storage with optional WORM retention."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str = "",
        region: str = "",
        kms_key_id: str = "",
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.kms_key_id = kms_key_id
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
        )

    def put_stream(
        self,
        namespace: str,
        filename: str,
        stream: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        retention_days: int | None = None,
        compliance_mode: bool = False,
    ) -> tuple[str, str]:
        digest = hashlib.sha256()
        # Hash before upload without retaining an unbounded document in process memory.
        # Small objects stay in memory; large documents spill to a private temp file and
        # boto3 transparently switches to multipart upload.
        with tempfile.SpooledTemporaryFile(
            max_size=8 * 1024 * 1024, mode="w+b"
        ) as body:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                body.write(chunk)
            body.seek(0)
            checksum = digest.hexdigest()
            safe_name = Path(filename).name
            key = "/".join(
                part
                for part in (
                    self.prefix,
                    namespace.strip("/"),
                    checksum[:16] + "_" + safe_name,
                )
                if part
            )
            request: dict = {
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "Metadata": {"sha256": checksum},
            }
            if self.kms_key_id:
                request.update(
                    ServerSideEncryption="aws:kms",
                    SSEKMSKeyId=self.kms_key_id,
                )
            if retention_days:
                request.update(
                    ObjectLockMode="COMPLIANCE" if compliance_mode else "GOVERNANCE",
                    ObjectLockRetainUntilDate=datetime.now(UTC)
                    + timedelta(days=retention_days),
                )
            extra_args = {
                name: value
                for name, value in request.items()
                if name not in {"Bucket", "Key"}
            }
            self.client.upload_fileobj(
                body,
                self.bucket,
                key,
                ExtraArgs=extra_args,
            )
        return key, checksum

    def download(self, key: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(target))
        return target
