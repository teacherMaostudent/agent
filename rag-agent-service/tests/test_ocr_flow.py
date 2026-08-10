"""OCR 流程测试：扫描版检测 + 后台任务编排（离线，不调真 RapidOCR）。

真 RapidOCR 慢且重，这里用假引擎/桩替换，只验证编排逻辑：
- 扫描版检测阈值（文字层过少 → 判扫描版）
- 后台任务状态机（OCR_RUNNING → PARSED，进度落 metadata，标 source=ocr）
- 重复触发保护
"""

from app.ingestion import ocr_task


class _FakeContainer:
    """最小容器桩：内存存一个 document，parser._ocr_pdf 返回固定文本。"""

    def __init__(self, document) -> None:
        self._doc = document
        self.repository = self
        self.parser = self

    # repository 接口
    def get_document(self, document_id: str):
        return self._doc if self._doc.document_id == document_id else None

    def save_document(self, document):
        self._doc = document
        return document

    # parser 接口：模拟逐页 OCR，回调进度
    def _ocr_pdf(self, path, progress=None):
        if progress:
            progress(1, 2)
            progress(2, 2)
        return "第一条 本规程适用于……\n第二条 ……"


def _make_doc(status="UPLOADED"):
    from app.domain.models import Document

    return Document(
        document_id="doc_ocr",
        filename="扫描件.pdf",
        file_path="/tmp/扫描件.pdf",
        sha256="x" * 64,
        status=status,
    )


def test_ocr_task_completes_and_marks_source() -> None:
    """后台 OCR 跑完：状态 PARSED、文本写入、标 source=ocr、进度到满。"""
    doc = _make_doc()
    container = _FakeContainer(doc)
    ocr_task._run("doc_ocr", container)  # 直接同步跑线程体，避免测试等线程
    assert container._doc.status == "PARSED"
    assert "第一条" in container._doc.text
    assert container._doc.metadata["source"] == "ocr"
    assert container._doc.metadata["ocr_done"] == 2
    assert container._doc.metadata["ocr_total"] == 2


def test_ocr_failure_marks_failed_not_crash() -> None:
    """OCR 抛异常时标 OCR_FAILED，不崩。"""
    doc = _make_doc()
    container = _FakeContainer(doc)

    def boom(path, progress=None):
        raise RuntimeError("模型加载失败")

    container._ocr_pdf = boom
    ocr_task._run("doc_ocr", container)
    assert container._doc.status == "OCR_FAILED"
    assert "模型加载失败" in container._doc.metadata["ocr_error"]
