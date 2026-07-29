"""扫描版 PDF 的后台 OCR 任务。见项目进展报告 OCR 一节。

为什么要后台任务:468 页 OCR 要数分钟,不能卡在上传/审查请求里。用独立线程跑
(不占 FastAPI web 线程),进度写进 document.metadata 并落库,前端轮询 status 端点
看"已识别 N/总页"。纯本地识别,不外传(保密件)。
"""
import logging
import threading

log = logging.getLogger(__name__)

# 正在 OCR 的文档 id,防止重复触发同一文档。
_running: set[str] = set()
_lock = threading.Lock()


def is_running(document_id: str) -> bool:
    with _lock:
        return document_id in _running


def start_ocr(document_id: str, container) -> bool:
    """启动后台 OCR。已在跑则返回 False(不重复触发),否则起线程并返回 True。"""
    with _lock:
        if document_id in _running:
            return False
        _running.add(document_id)
    thread = threading.Thread(
        target=_run, args=(document_id, container), daemon=True
    )
    thread.start()
    return True


def _run(document_id: str, container) -> None:
    """后台线程体:逐页 OCR,进度落库,完成/失败更新状态。"""
    try:
        document = container.repository.get_document(document_id)
        if document is None:
            return
        document.status = "OCR_RUNNING"
        document.metadata["ocr_done"] = 0
        container.repository.save_document(document)

        def progress(done: int, total: int) -> None:
            document.metadata["ocr_done"] = done
            document.metadata["ocr_total"] = total
            # 每页落库一次,让 status 端点能实时看到进度。SQLite 写很快,468 次可接受。
            container.repository.save_document(document)

        text = container.parser._ocr_pdf(document.file_path, progress=progress)
        document.text = text
        document.status = "PARSED"
        document.metadata["parser"] = "rapidocr"
        document.metadata["source"] = "ocr"  # 标记来源:下游报告须提示"OCR识别,可能有误"
        container.repository.save_document(document)
        log.info("OCR 完成 document_id=%s 共 %d 字", document_id, len(text))
    except Exception as exc:  # OCR 失败不崩服务,标状态供前端提示
        log.warning("OCR 失败 document_id=%s: %s", document_id, exc)
        document = container.repository.get_document(document_id)
        if document is not None:
            document.status = "OCR_FAILED"
            document.metadata["ocr_error"] = str(exc)
            container.repository.save_document(document)
    finally:
        with _lock:
            _running.discard(document_id)
