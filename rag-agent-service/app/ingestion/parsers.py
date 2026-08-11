from pathlib import Path
from typing import Any

from app.ingestion.file_detector import detect_file_type


class DocumentParser:
    """Best-effort parser with optional heavy dependencies kept at module boundaries."""

    def parse(self, path: Path) -> tuple[str, dict[str, Any]]:
        """按受控文件类型提取文本及来源元数据，但不直接写文档或索引。

        缺失可选解析依赖会显式失败，使队列记录可重试错误，而不是产生空证据。
        """
        file_type = detect_file_type(path)
        if file_type in {"text", "markdown"}:
            return path.read_text(encoding="utf-8", errors="ignore"), {"parser": file_type}
        if file_type == "pdf":
            return self._parse_pdf(path), {"parser": "pymupdf"}
        if file_type == "word":
            return self._parse_word(path), {"parser": "python-docx"}
        if file_type == "excel":
            return self._parse_excel(path), {"parser": "pandas"}
        if file_type == "image":
            # Images are first-class evidence sources: OCR output is retained
            # with a provenance marker so downstream prompts can distinguish
            # recognized text from author-provided digital text.
            text = self._ocr_image(path)
            return text, {
                "parser": "rapidocr",
                "source_modality": "image",
                "evidence_quality": "ocr_extracted",
                "requires_visual_review": True,
            }
        return path.read_text(encoding="utf-8", errors="ignore"), {"parser": "fallback-text"}

    def _parse_pdf(self, path: Path) -> str:
        """读取 PDF 文本层；疑似扫描件才降级为本地 OCR，避免常规文档高开销。"""
        try:
            import fitz  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PDF parsing requires pymupdf. Install project dependencies first."
            ) from exc
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text("text") for page in doc)
            page_count = doc.page_count
        # 扫描版检测:有文字层的正常 PDF 直接返回;文字层过少(平均<20字/页)判为
        # 扫描版,走 OCR。注意:OCR 468 页要数分钟,parse() 是同步的——大文件应由
        # 后台任务调 _ocr_pdf,不要在请求里同步跑。此处兜底供小扫描件直接用。
        if page_count > 0 and len(text.strip()) / page_count < 20:
            return self._ocr_pdf(path)
        return text

    def _ocr_pdf(self, path: Path, progress=None) -> str:
        """扫描版 PDF 逐页渲染成图 → 本地 RapidOCR 识别 → 拼接全文。

        progress: 可选回调 progress(done, total),供后台任务上报进度。
        纯本地识别,不外传(保密件)。表格页文字准但行列会乱序(已知限制)。
        """
        import fitz  # type: ignore
        import numpy as np  # type: ignore

        from app.ingestion.ocr_engine import OcrEngine

        engine = OcrEngine.instance()
        pages_text: list[str] = []
        with fitz.open(path) as doc:
            total = doc.page_count
            for i, page in enumerate(doc):
                # 渲染成图(放大 2x 提升小字识别率),转 numpy 喂 RapidOCR。
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                # RapidOCR 要 3 通道 BGR;若含 alpha 通道(n==4)则丢弃。
                if pix.n == 4:
                    img = img[:, :, :3]
                pages_text.append(engine.recognize_image(img[:, :, ::-1]))
                if progress:
                    progress(i + 1, total)
        return "\n".join(pages_text)

    def _ocr_image(self, path: Path) -> str:
        """本地提取图片 OCR 文本，并明确将其标记为可检索的派生证据。

        原始图片仍是对象存储中的权威载体；未经人工视觉复核，OCR 文本不得被
        表述为像素级准确的原件转写。
        """
        try:
            import numpy as np  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise RuntimeError("image OCR requires Pillow and numpy") from exc
        from app.ingestion.ocr_engine import OcrEngine

        with Image.open(path) as image:
            # RapidOCR expects BGR/RGB-like arrays; converting removes palette
            # and alpha variability before the local engine receives pixels.
            pixels = np.asarray(image.convert("RGB"))
        return OcrEngine.instance().recognize_image(pixels[:, :, ::-1])

    def _parse_word(self, path: Path) -> str:
        """提取 Word 段落；缺少 python-docx 时显式中断摄取任务。"""
        try:
            from docx import Document as WordDocument  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Word parsing requires python-docx. Install project dependencies first."
            ) from exc
        doc = WordDocument(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def _parse_excel(self, path: Path) -> str:
        """逐表页转为可检索 Markdown，保留 sheet 边界而不臆造表格语义。"""
        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Excel parsing requires pandas/openpyxl. Install project dependencies first."
            ) from exc
        frames = (
            pd.read_excel(path, sheet_name=None)
            if path.suffix.lower() != ".csv"
            else {"sheet1": pd.read_csv(path)}
        )
        lines: list[str] = []
        for sheet, frame in frames.items():
            lines.append(f"# Sheet: {sheet}")
            lines.append(frame.fillna("").to_markdown(index=False))
        return "\n\n".join(lines)
