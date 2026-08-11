import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings


class LocalFileStorage:
    """Simple filesystem storage; replace with MinIO without touching API code."""

    def __init__(self, settings: Settings) -> None:
        """持有已验证的目录配置；本地文件系统只用于开发或单节点部署。"""
        self.settings = settings

    def save_upload(self, filename: str, stream: BinaryIO) -> tuple[Path, str]:
        """流式落盘并计算 SHA-256，再以摘要命名以减少同名文件覆盖风险。"""
        safe_name = Path(filename).name
        target = self.settings.upload_dir / safe_name
        digest = hashlib.sha256()
        with target.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        sha256 = digest.hexdigest()
        final_path = self.settings.upload_dir / f"{sha256[:16]}_{safe_name}"
        if target != final_path:
            shutil.move(str(target), str(final_path))
        return final_path, sha256

    def save_report(self, review_id: str, markdown: str) -> Path:
        """保存本地报告副本；不可变审计导出应由对象存储/WORM 实现。"""
        path = self.settings.report_dir / f"{review_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    @staticmethod
    def object_key_for(path: Path) -> None:
        """本地存储没有远端对象键，统一返回 None 供调用方安全分支。"""
        del path
        return None

    @staticmethod
    def materialize(path: Path, metadata: dict) -> Path:
        """本地文件已可被解析器读取，忽略远端对象元数据。"""
        del metadata
        return path
