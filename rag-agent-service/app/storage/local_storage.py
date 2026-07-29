import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings


class LocalFileStorage:
    """Simple filesystem storage; replace with MinIO without touching API code."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def save_upload(self, filename: str, stream: BinaryIO) -> tuple[Path, str]:
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
        path = self.settings.report_dir / f"{review_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

