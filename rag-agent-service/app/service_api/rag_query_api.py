from fastapi import APIRouter, HTTPException, Request

from app.contracts.rag import (
    ControlledScanRequest,
    ControlledScanResponse,
    RagCapabilitiesResponse,
    RagIndexVersionResponse,
    RagSearchRequest,
    RagSearchResponse,
)
from app.retrieval.controlled_scan import (
    ControlledScanRequestError,
    ControlledScanUnavailableError,
)

router = APIRouter(prefix="/query", tags=["rag-query"])


@router.get("/capabilities", response_model=RagCapabilitiesResponse)
def capabilities() -> RagCapabilitiesResponse:
    """公开受支持的 RAG 契约能力，不泄露内部模块、实现或部署细节。"""
    return RagCapabilitiesResponse()


@router.get("/index-version", response_model=RagIndexVersionResponse)
def index_version(request: Request) -> RagIndexVersionResponse:
    """暴露当前不可变索引版本，供发布流程和 Runtime 执行前进行一致性校验。"""
    query_service = request.app.state.container.query_service
    return RagIndexVersionResponse(
        index_version=query_service.index_version,
        backend=query_service.backend,
        embedding_contract=query_service.embedding_contract,
    )


@router.post("/search", response_model=RagSearchResponse)
def search(payload: RagSearchRequest, request: Request) -> RagSearchResponse:
    """查询已发布索引中的授权证据；该入口不接受写操作或 Agent 决策指令。"""
    return request.app.state.container.query_service.search(payload)


@router.post("/scan", response_model=ControlledScanResponse)
def scan(payload: ControlledScanRequest, request: Request) -> ControlledScanResponse:
    """扫描配置允许的日志/源码/文本目录，并返回经过脱敏和条数限制的命中。"""
    try:
        matches = request.app.state.container.query_service.scan(
            payload.scope, payload.pattern, regex=payload.regex, glob=payload.glob
        )
    except ControlledScanRequestError as exc:
        # 工具参数来自模型，必须给出可供下一轮 Decision 修正的稳定反馈；把它抛成
        # 500 会触发无意义重试，并最终消耗完整个 Runtime 尝试预算。
        raise HTTPException(
            status_code=422,
            detail={
                "code": "controlled_scan_arguments_invalid",
                "message": str(exc),
                "hint": "Remove advanced regex constructs or set regex=false for literal matching.",
            },
        ) from exc
    except ControlledScanUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "controlled_scan_unavailable", "message": str(exc)},
        ) from exc
    return ControlledScanResponse(scope=payload.scope, matches=matches)
