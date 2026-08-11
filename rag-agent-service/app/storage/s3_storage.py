"""S3-backed document storage with temporary local materialization for parsers."""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from platform_infra.object_storage import S3ObjectStorage

from app.core.config import Settings


class S3FileStorage:
    """Keep raw file bytes outside the RAG index and retrieve them on ingestion."""

    def __init__(self, settings: Settings) -> None:
        """初始化对象存储与进程内键映射；持久化对象键仍写入文档元数据。"""
        self.settings = settings
        self.objects = S3ObjectStorage(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            kms_key_id=settings.s3_kms_key_id,
        )
        self._keys: dict[str, str] = {}
        self._lock = Lock()

    def save_upload(self, filename: str, stream: BinaryIO) -> tuple[Path, str]:
        """上传原件至对象存储，同时建立临时解析副本并返回内容摘要。"""
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as buffered:
            shutil.copyfileobj(stream, buffered, length=1024 * 1024)
            buffered.seek(0)
            key, sha256 = self.objects.put_stream("documents", filename, buffered)
            target = self.settings.upload_dir / f"{sha256[:16]}_{Path(filename).name}"
            buffered.seek(0)
            with target.open("wb") as local_copy:
                shutil.copyfileobj(buffered, local_copy, length=1024 * 1024)
        with self._lock:
            self._keys[str(target)] = key
        return target, sha256

    def object_key_for(self, path: Path) -> str | None:
        """查找当前进程的对象键；重启后必须以文档元数据中的 object_key 为准。"""
        with self._lock:
            return self._keys.get(str(path))

    def materialize(self, path: Path, metadata: dict) -> Path:
        """确保解析器有本地文件；缓存缺失时从已持久化 object_key 下载。"""
        if path.exists():
            return path
        object_key = metadata.get("object_key")
        if not isinstance(object_key, str) or not object_key:
            raise FileNotFoundError(f"document object key is unavailable: {path}")
        return self.objects.download(object_key, path)

    def save_report(self, review_id: str, markdown: str) -> Path:
        """上传报告并保留本地副本；远端对象才是跨节点共享的来源。"""
        encoded = markdown.encode()
        key, _ = self.objects.put_stream(
            "reports",
            f"{review_id}.md",
            io.BytesIO(encoded),
            content_type="text/markdown; charset=utf-8",
        )
        path = self.settings.report_dir / f"{review_id}.md"
        path.write_bytes(encoded)
        with self._lock:
            self._keys[str(path)] = key
        return path
