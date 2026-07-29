from pathlib import Path


def detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".docx", ".doc"}:
        return "word"
    if suffix in {".xlsx", ".xls", ".csv"}:
        return "excel"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".txt", ".text"}:
        return "text"
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}:
        return "image"
    return "unknown"

