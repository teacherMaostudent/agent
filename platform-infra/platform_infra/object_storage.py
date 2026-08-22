"""Shared S3-compatible object-storage primitives for durable platform artifacts."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import boto3


class S3ObjectStorage:
    """Write content-addressed tenant objects with optional WORM retention semantics."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str = "",
        region: str = "",
        kms_key_id: str = "",
    ) -> None:
        """创建 S3 客户端；桶、前缀和 KMS key 只能由部署配置确定。"""
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
        """流式写入内容寻址对象，并返回对象键与完整 SHA-256。

        文件名会去路径化以防目录穿越；可选 Object Lock 用于不可变审计导出，调用方
        必须保证目标桶已启用对应保留能力。
        """
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
        """下载指定对象到调用者给定路径；访问控制由 S3 身份策略承担。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(target))
        return target

    def presign_download(self, key: str, *, expires_seconds: int = 300) -> str:
        """签发指定对象的短期只读 URL；调用方必须先完成业务资源授权。

        该方法不能接收完整 URL，以免被误用成任意主机跳转或跨桶数据外带。S3 IAM
        仍是最终数据面边界；签名仅缩短已授权浏览器下载的凭据生命周期。
        """
        normalized_key = key.strip().lstrip("/")
        if not normalized_key or ".." in Path(normalized_key).parts:
            raise ValueError("invalid object storage key")
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": normalized_key},
            ExpiresIn=min(max(expires_seconds, 30), 900),
        )

    def read_bounded(self, key: str, *, max_bytes: int) -> tuple[bytes, bool]:
        """Read a bounded leading range for preview without becoming an object proxy."""
        normalized_key = key.strip().lstrip("/")
        if not normalized_key or ".." in Path(normalized_key).parts:
            raise ValueError("invalid object storage key")
        bounded = min(max(max_bytes, 1), 1_048_576)
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=normalized_key,
            Range=f"bytes=0-{bounded}",
        )
        body = response["Body"]
        try:
            payload = body.read(bounded + 1)
        finally:
            body.close()
        return payload[:bounded], len(payload) > bounded
