from app.domain.models import (
    AlcoaRisk,
    ChecklistItem,
    Chunk,
    DataIntegrityFieldCheck,
    Document,
    Regulation,
    ReviewResult,
)
from app.ingestion.chunker import TextChunker
from app.knowledge.config_loader import (
    config_version,
    load_alcoa_risks,
    load_checklist,
    load_field_checks,
    load_term_groups,
    load_type_mapping,
    load_vague_words,
    mapping_diagnostics,
    requirement_ids_for,
    resolve_requirements,
    validate_knowledge_config,
)
from app.knowledge.seed_data import DEFAULT_REGULATIONS


class InMemoryRepository:
    """Development repository; production can swap this for PostgreSQL/pgvector."""

    def __init__(self) -> None:
        validate_knowledge_config()
        self.documents: dict[str, Document] = {}
        self.regulations: dict[str, Regulation] = {item.regulation_id: item for item in DEFAULT_REGULATIONS}
        # 清单从 config(13 条移植资产)加载，取代原来硬编码的 3 条。
        self.checklist: dict[str, ChecklistItem] = {item.requirement_id: item for item in load_checklist()}
        self._field_checks: list[DataIntegrityFieldCheck] = load_field_checks()
        self._alcoa_risks: list[AlcoaRisk] = load_alcoa_risks()
        self.reviews: dict[str, ReviewResult] = {}
        self._chunker = TextChunker()

    def data_integrity_fields(self) -> list[DataIntegrityFieldCheck]:
        return self._field_checks

    def data_integrity_risks(self) -> list[AlcoaRisk]:
        return self._alcoa_risks

    def document_type_tree(self) -> dict:
        """两级分类树 {模块: [二级分类...]}，给业务前端的分类选择器用。"""
        return load_type_mapping()["modules"]

    def clarity_vague_words(self) -> list[dict]:
        return load_vague_words()

    def clarity_term_groups(self) -> list[dict]:
        return load_term_groups()

    def checklist_version(self) -> str:
        return config_version()

    def checklist_for_document_type(self, document_type: str | None) -> list[ChecklistItem]:
        """按二级分类过滤清单：只返回该类文件应核查的条目。

        这是避免"无意义 MISSING"的关键——一份场地管理文件不该被要求写偏差流程。
        未指定或未命中分类时，走 _default 兜底(文件控制 + 记录控制)。
        """
        target_ids = requirement_ids_for(document_type)
        items = [self.checklist[req_id] for req_id in target_ids if req_id in self.checklist]
        return items or list(self.checklist.values())

    def checklist_selection(self, document_type: str | None) -> tuple[list[ChecklistItem], dict]:
        resolution = resolve_requirements(document_type)
        items = [
            self.checklist[req_id]
            for req_id in resolution["requirement_ids"]
            if req_id in self.checklist
        ]
        return items, resolution

    def mapping_diagnostics(self) -> dict:
        return mapping_diagnostics()

    def save_document(self, document: Document) -> Document:
        self.documents[document.document_id] = document
        return document

    def get_document(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)

    def save_regulations(self, regulations: list[Regulation]) -> None:
        for item in regulations:
            self.regulations[item.regulation_id] = item

    def regulation_chunks(self) -> list[Chunk]:
        chunks: list[Chunk] = []
        for regulation in self.regulations.values():
            text = f"{regulation.standard} {regulation.clause_no} {regulation.title}\n{regulation.content}"
            chunks.extend(
                self._chunker.chunk(
                    source_id=regulation.regulation_id,
                    source_type="regulation",
                    text=text,
                    metadata={**regulation.metadata, "standard": regulation.standard, "clause_no": regulation.clause_no},
                )
            )
        return chunks

    def document_chunks(self, document_id: str) -> list[Chunk]:
        document = self.documents[document_id]
        return self._chunker.chunk(document.document_id, "enterprise_document", document.text, document.metadata)

    def save_review(self, review: ReviewResult) -> ReviewResult:
        self.reviews[review.review_id] = review
        return review

    def get_review(self, review_id: str) -> ReviewResult | None:
        return self.reviews.get(review_id)

