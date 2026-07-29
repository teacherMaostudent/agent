from typing import Any

from pydantic import BaseModel, Field


class ParseResponse(BaseModel):
    document_id: str
    status: str
    text_length: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class GmpReviewRequest(BaseModel):
    document_id: str | None = None
    content: str | None = None
    document_type: str = "gmp_document"
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeImportRequest(BaseModel):
    items: list[dict[str, Any]]


class GenerateDocumentRequest(BaseModel):
    """逆向生成请求:选文件类型 + 可选补充说明 + 可选参照文件。"""

    document_type: str
    supplement: str = ""
    # 可选:先经 /documents/upload 拿到 document_id,生成时参照其内容重写。
    reference_document_id: str | None = None
    # 是否开启"生成→自检→自动修订"闭环(默认开)。
    revise: bool = True


class ExportDocxRequest(BaseModel):
    """把 Markdown 文本导出成 Word(.docx)。"""

    markdown: str
    title: str = ""  # 文档首行大标题,空则不加
    filename: str = ""  # 下载文件名(不含扩展名也行),空则用默认
    highlight: list[str] = Field(
        default_factory=list
    )  # 正文中加粗+黄底标出的词(问题点)


class CrossDocumentReviewRequest(BaseModel):
    """跨文档审查:传多份已上传文件的 document_id,做矛盾/职责冲突分析。"""

    document_ids: list[str] = Field(default_factory=list)


class AnnotateFindingRequest(BaseModel):
    """对某条跨文档发现的人工标注(快照式,不回流)。"""

    verdict: str = ""  # confirmed(确认为真) | rejected(否决/排除) | ""(清除)
    note: str = ""  # 处理意见/排除原因


class AgentRunRequest(BaseModel):
    task: str
    agent_id: str = Field(default="gmp-review-agent", min_length=2, max_length=160)
    environment: str = Field(default="production", min_length=2, max_length=64)
    document_id: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, max_length=160)
    max_steps: int | None = Field(default=None, ge=2, le=30)
    deadline_seconds: int | None = Field(default=None, ge=1, le=600)
    attempt_budget: int | None = Field(default=None, ge=0, le=100)
    max_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class AgentResumeRequest(BaseModel):
    approved: bool
    approval_id: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=2_000)
