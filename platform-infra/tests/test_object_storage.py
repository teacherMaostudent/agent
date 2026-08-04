from __future__ import annotations

import io

from platform_infra.object_storage import S3ObjectStorage


class RecordingS3Client:
    def __init__(self) -> None:
        self.uploaded = b""
        self.bucket = ""
        self.key = ""
        self.extra_args: dict = {}

    def upload_fileobj(self, stream, bucket, key, ExtraArgs):
        self.uploaded = stream.read()
        self.bucket = bucket
        self.key = key
        self.extra_args = ExtraArgs


def test_put_stream_hashes_and_uploads_with_retention() -> None:
    storage = object.__new__(S3ObjectStorage)
    storage.bucket = "audit"
    storage.prefix = "worm"
    storage.kms_key_id = "kms-key"
    storage.client = RecordingS3Client()

    key, checksum = storage.put_stream(
        "tenant-a",
        "../events.json",
        io.BytesIO(b"immutable-audit"),
        content_type="application/json",
        retention_days=7,
        compliance_mode=True,
    )

    assert checksum == "1c9a6449eb877871d786a1602878944bd872e76b20673edbb2e355a2b40bd0db"
    assert key == f"worm/tenant-a/{checksum[:16]}_events.json"
    assert storage.client.uploaded == b"immutable-audit"
    assert storage.client.extra_args["ObjectLockMode"] == "COMPLIANCE"
    assert storage.client.extra_args["ServerSideEncryption"] == "aws:kms"
