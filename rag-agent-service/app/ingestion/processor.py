"""Document ingestion pipeline from stored object to indexed knowledge chunks."""

from app.contracts.ingestion import IngestionJob


class IngestionJobProcessor:
    """Keep source persistence authoritative while publishing a rebuildable index."""

    def __init__(self, container) -> None:
        """注入摄取容器，使处理器共享权威存储、索引投影和事件发布依赖。"""
        self.container = container

    def process(self, job: IngestionJob) -> dict:
        """按任务类型分派处理器；未知类型立即失败，避免被标记为伪成功。"""
        handlers = {
            "PARSE": self._parse,
            "OCR": self._ocr,
        }
        handler = handlers.get(job.job_type)
        if handler is None:
            raise ValueError(f"unsupported ingestion job type: {job.job_type}")
        return handler(job)

    def _document(self, job: IngestionJob):
        """读取任务绑定文档；摄取任务不得通过任意路径绕过文档主记录。"""
        if not job.document_id:
            raise ValueError(f"{job.job_type} requires document_id")
        document = self.container.repository.get_document(job.document_id)
        if document is None:
            raise ValueError("document not found")
        return document

    def _parse(self, job: IngestionJob) -> dict:
        """解析原始对象、保存权威文档，再刷新可重建的检索投影。

        先保存解析结果再建立索引，使索引失败时仍能以文档库为事实来源重放。
        """
        document = self._document(job)
        path = self.container.storage.materialize(document.file_path, document.metadata)
        text, metadata = self.container.parser.parse(path)
        document.text = text
        document.metadata.update(metadata)
        document.metadata.setdefault(
            "evidence_provenance",
            {
                "artifact_sha256": document.sha256,
                "parser": metadata.get("parser", "unknown"),
                "source_modality": metadata.get("source_modality", "text"),
                "derived_at": "ingestion",
            },
        )
        document.status = "PARSED"
        self.container.repository.save_document(document)
        self.container.search_projection.index_document(
            document,
            self.container.repository.document_chunks(document.document_id),
        )
        return {"document_id": document.document_id, "text_length": len(text)}

    def _ocr(self, job: IngestionJob) -> dict:
        """执行显式 OCR 任务并持久化进度；OCR 结果标记为派生证据而非原件。"""
        document = self._document(job)
        document.status = "OCR_RUNNING"
        self.container.repository.save_document(document)

        def progress(done: int, total: int) -> None:
            """将 OCR 进度写回文档元数据，供客户端轮询且允许 Worker 故障后恢复观察。"""
            document.metadata.update({"ocr_done": done, "ocr_total": total})
            self.container.repository.save_document(document)

        path = self.container.storage.materialize(document.file_path, document.metadata)
        text = self.container.parser._ocr_pdf(path, progress=progress)
        document.text = text
        document.status = "PARSED"
        document.metadata.update({"parser": "rapidocr", "source": "ocr"})
        self.container.repository.save_document(document)
        self.container.search_projection.index_document(
            document,
            self.container.repository.document_chunks(document.document_id),
        )
        return {"document_id": document.document_id, "text_length": len(text)}
