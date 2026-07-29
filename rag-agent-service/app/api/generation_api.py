"""逆向生成接口:选文件类型 + 补充说明 → 带法规依据的初稿 + 自检。"""
import logging

from fastapi import APIRouter, HTTPException

from app.domain.schemas import GenerateDocumentRequest
from app.services import container

log = logging.getLogger(__name__)

router = APIRouter(prefix="/generation", tags=["generation"])


@router.post("/document")
def generate_document(request: GenerateDocumentRequest) -> dict:
    if container.generator is None:
        raise HTTPException(
            status_code=503,
            detail="逆向生成需要 llm-gateway:请启用 RAG_LLM_ENABLED 并配置网关地址和逻辑模型。",
        )
    if not request.document_type.strip():
        raise HTTPException(status_code=400, detail="document_type 不能为空")

    # 可选参照文件:取已上传文档的正文(需要时先解析),让模型在其基础上重写。
    reference_text = ""
    if request.reference_document_id:
        document = container.repository.get_document(request.reference_document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="参照文件不存在,请重新上传")
        if not document.text:
            text, metadata = container.parser.parse(document.file_path)
            document.text = text
            document.status = "PARSED"
            document.metadata.update(metadata)
            container.repository.save_document(document)
        reference_text = document.text

    try:
        result = container.generator.generate(
            document_type=request.document_type,
            supplement=request.supplement,
            reference_text=reference_text,
            revise=request.revise,
        )
    except Exception as exc:  # 模型调用失败:网关不通、密钥错误、超时等
        log.warning("逆向生成失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"大模型生成失败:{exc}") from exc
    return {
        "document_type": result.document_type,
        "supplement": result.supplement,
        "requirements_used": result.requirements_used,
        "regulation_refs": result.regulation_refs,
        "content_markdown": result.content_markdown,
        "used_reference": result.used_reference,
        "revision_rounds": result.revision_rounds,
        "risk_trace": result.risk_trace,
        "remaining_issues": result.remaining_issues,
        "highlight_terms": result.highlight_terms,
        "self_check": {
            "summary": result.self_check_summary,
            "overall_risk": result.self_check_overall_risk,
            "missing": result.self_check_missing,
        },
    }
