from fastapi import APIRouter

from app.domain.models import Regulation
from app.domain.schemas import KnowledgeImportRequest
from app.services import container

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/regulations")
def list_regulations() -> list[Regulation]:
    return list(container.repository.regulations.values())


@router.post("/regulations/import")
def import_regulations(request: KnowledgeImportRequest) -> dict:
    regulations = [Regulation(**item) for item in request.items]
    container.repository.save_regulations(regulations)
    return {"imported": len(regulations)}


@router.get("/checklists")
def list_checklist() -> list:
    return list(container.repository.checklist.values())


@router.get("/document-types")
def document_types() -> dict:
    """返回正大天晴两级分类树 {模块: [二级分类...]}，供业务前端分类选择器使用。"""
    return {
        "version": container.repository.checklist_version(),
        "tree": container.repository.document_type_tree(),
        "mapping": container.repository.mapping_diagnostics(),
    }


@router.post("/reindex")
def reindex_regulations() -> dict:
    """建法规库：读纳入的 5 部法规 PDF → 切块 → embedding → 存库 → 落缓存。

    embedding 只在这里算一次(provider=qwen 时会调通义 API，产生费用)，
    之后检索直接用缓存。返回每部法规的切块数与状态。
    """
    return container.build_regulation_library()


@router.get("/stats")
def knowledge_stats() -> dict:
    """法规库状态：是否已建、片段数、用的哪个 embedder。"""
    return container.regulation_stats()

