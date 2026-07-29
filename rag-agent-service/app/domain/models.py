from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CoverageStatus(StrEnum):
    COVERED = "COVERED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNCERTAIN = "UNCERTAIN"


class JudgeMethod(StrEnum):
    LLM = "LLM"
    KEYWORD_FALLBACK = "KEYWORD_FALLBACK"
    NO_EVIDENCE = "NO_EVIDENCE"
    CONFIG_ERROR = "CONFIG_ERROR"


# 中文严重度 → RiskLevel。配置 JSON 里用"高/中/低"，内部统一成枚举。
_SEVERITY_MAP = {
    "高": RiskLevel.HIGH,
    "中": RiskLevel.MEDIUM,
    "低": RiskLevel.LOW,
    "严重": RiskLevel.CRITICAL,
}


def severity_from_zh(value: str | RiskLevel | None) -> RiskLevel:
    if isinstance(value, RiskLevel):
        return value
    if not value:
        return RiskLevel.MEDIUM
    return _SEVERITY_MAP.get(str(value).strip(), RiskLevel.MEDIUM)


class Document(BaseModel):
    document_id: str = Field(default_factory=lambda: new_id("doc"))
    filename: str
    content_type: str | None = None
    file_path: Path
    sha256: str
    status: str = "UPLOADED"
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chk"))
    source_id: str
    source_type: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Regulation(BaseModel):
    regulation_id: str
    standard: str
    clause_no: str
    title: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChecklistItem(BaseModel):
    requirement_id: str
    module: str
    dimension: str
    title: str
    description: str
    severity: RiskLevel = RiskLevel.MEDIUM
    required_fields: list[str] = Field(default_factory=list)
    regulation_refs: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    reviewer: str = "coverage"
    level: str = "common"  # common | module | document_type
    applicable_document_types: list[str] = Field(default_factory=list)
    applicability: str = ""
    synonyms: list[str] = Field(default_factory=list)
    positive_examples: list[str] = Field(default_factory=list)
    negative_examples: list[str] = Field(default_factory=list)
    review_method: str = "field_extraction_and_regulation_retrieval"


class Evidence(BaseModel):
    source_id: str
    source_type: str
    text: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class DimensionReview(BaseModel):
    requirement_id: str
    dimension: str
    title: str
    passed: bool
    coverage_status: CoverageStatus = CoverageStatus.UNCERTAIN
    judge_method: JudgeMethod = JudgeMethod.KEYWORD_FALLBACK
    degraded: bool = False
    degrade_reason: str | None = None
    risk_level: RiskLevel
    missing_points: list[str] = Field(default_factory=list)
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)  # 企业文件里的证据(判有没有写)
    regulation_evidence: list[Evidence] = Field(default_factory=list)  # 法规库里的对应条文(依据引用)
    regulation_refs: list[str] = Field(default_factory=list)
    capa_suggestion: str
    need_human_review: bool


class CoverageMetrics(BaseModel):
    covered: int = 0
    partial: int = 0
    missing: int = 0
    not_applicable: int = 0
    uncertain: int = 0
    denominator: int = 0
    rate: float | None = None


class ReviewResult(BaseModel):
    review_id: str = Field(default_factory=lambda: new_id("rev"))
    document_id: str
    status: str = "COMPLETED"
    summary: str
    overall_risk: RiskLevel
    dimensions: list[DimensionReview]
    coverage: CoverageMetrics = Field(default_factory=CoverageMetrics)
    mapping_status: str = "UNSPECIFIED"
    mapping_warning: str | None = None
    data_integrity: "DataIntegrityReport | None" = None
    clarity: "ClarityReport | None" = None
    standard_floor: "StandardFloorReport | None" = None
    report_markdown: str
    report_path: Path | None = None
    created_at: datetime = Field(default_factory=utc_now)


# --- 数据可靠性(ALCOA+)模型:对应 data-integrity-checklist.json 的两部分 ---


class DataIntegrityFieldCheck(BaseModel):
    """ALCOA+ 关键字段检查项(fieldChecks 一条)。"""

    id: str
    field: str
    requirement: str
    keywords: list[str] = Field(default_factory=list)
    severity: RiskLevel = RiskLevel.MEDIUM


class AlcoaRisk(BaseModel):
    """ALCOA+ 风险排查项(alcoaRisks 一条)。"""

    id: str
    principle: str
    risk: str
    requirement: str
    red_flags: list[str] = Field(default_factory=list)
    expect: list[str] = Field(default_factory=list)
    severity: RiskLevel = RiskLevel.MEDIUM


class FieldCheckResult(BaseModel):
    id: str
    field: str
    present: str  # PRESENT | MISSING | UNKNOWN
    severity: RiskLevel
    evidence: str = ""
    comment: str = ""


class AlcoaRiskResult(BaseModel):
    id: str
    principle: str
    risk: str
    verdict: str  # OK | RISK | UNCLEAR
    severity: RiskLevel
    evidence: str = ""
    comment: str = ""


class DataIntegrityReport(BaseModel):
    verdict: str
    judge_method: JudgeMethod = JudgeMethod.KEYWORD_FALLBACK
    degraded: bool = False
    degrade_reason: str | None = None
    field_total: int
    field_present: int
    field_missing: int
    critical_missing_fields: list[str] = Field(default_factory=list)
    risk_total: int
    risk_found: int
    found_risks: list[str] = Field(default_factory=list)
    fields: list[FieldCheckResult] = Field(default_factory=list)
    risks: list[AlcoaRiskResult] = Field(default_factory=list)


# --- 表述清晰度模型(设计文档 3.7):对应 clarity-config.json ---


class VagueFinding(BaseModel):
    """一处模糊表述命中。"""

    word: str
    suggestion: str
    severity: RiskLevel
    context: str  # 命中词所在的上下文片段


class TermInconsistency(BaseModel):
    """一个术语在文件中出现了多种写法(应统一)。"""

    canonical: str  # 标准术语
    variants_found: list[str]  # 文件中实际出现的多种写法
    note: str


class ClarityReport(BaseModel):
    verdict: str
    vague_count: int
    vague_findings: list[VagueFinding] = Field(default_factory=list)
    term_issue_count: int
    term_inconsistencies: list[TermInconsistency] = Field(default_factory=list)


# --- 标准底线核查(3.2a)：企业量化标准不得低于法规底线 ---


class StandardFloorFinding(BaseModel):
    """一条企业量化标准 vs 法规底线的判定。verdict 三态。"""

    verdict: str  # PASS(达标) | FAIL(低于底线) | UNKNOWN(无法自动判定,交人工)
    attribute: str  # 企业断言的属性(如"纯化水微生物限度")
    enterprise_value: str  # 企业文件里的取值(如"不超过150 CFU/mL")
    floor_value: str = ""  # 法规底线值(含单位,展示用)
    source: str = ""  # 底线的法规来源
    reason: str = ""  # 判定说明
    quote: str = ""  # 企业原文摘录,可复核
    need_human_review: bool = True  # FAIL/UNKNOWN 默认需人工确认


class StandardFloorReport(BaseModel):
    verdict: str = ""  # 一句话结论
    judge_method: JudgeMethod = JudgeMethod.KEYWORD_FALLBACK
    checked_count: int = 0  # 判定的断言条数
    fail_count: int = 0  # 低于底线的条数
    unknown_count: int = 0  # 无法自动判定的条数
    findings: list[StandardFloorFinding] = Field(default_factory=list)


# --- 跨文档聚合(组件⑥，设计文档 3.2b/3.5)：多份企业文件放一起找矛盾/职责冲突 ---


class CrossDocEvidence(BaseModel):
    """一条矛盾/冲突涉及的单方证据：来自哪份文件、原话是什么。可复核(Q7)。"""

    document_id: str
    filename: str
    quote: str  # 该文件的原话摘录，供用户跳回核实


class CrossDocFinding(BaseModel):
    """一条跨文档发现(矛盾或职责问题)。默认都需人工确认(GMP human-review 闭环)。

    身份与展示分离(修尺子刻度问题)：
    - obj + document_pair 是【稳定身份】——矛盾的物理本质是"这两份文件在这个受控
      对象上打架"。命中判定、去重都用它，不受模型自由发挥的属性名影响。
    - topic/summary 是【展示信息】，给人看，要具体，但不参与身份匹配。
    """

    finding_type: str  # consistency(标准矛盾) | responsibility(职责问题)
    local_id: str = ""  # 仅在本次快照内唯一的标识，供人工标注定位(不跨快照对齐)
    obj: str = ""  # 受控对象(归一后)，如"阴凉库"——稳定身份的一半
    document_pair: list[str] = Field(default_factory=list)  # 排序后的文件对——身份的另一半
    topic: str = ""  # 展示用属性名(模型抽的，可具体，如"悬浮粒子监测频次")
    severity: RiskLevel = RiskLevel.MEDIUM
    summary: str = ""  # 一句话说清问题(展示用)
    evidence: list[CrossDocEvidence] = Field(default_factory=list)
    detail: str = ""  # 补充说明(如冲突类型：空缺/重叠/职责不分离)
    need_human_review: bool = True
    # 快照式人工标注(不做自动回流)：只属于本次快照，随报告归档。
    human_verdict: str = ""  # ""(未处理) | confirmed(确认为真) | rejected(已排除)
    human_note: str = ""  # 人工备注/处理意见


class CrossDocReport(BaseModel):
    snapshot_id: str = Field(default_factory=lambda: new_id("xdoc"))  # 本次审查快照 id
    created_at: datetime = Field(default_factory=utc_now)
    verdict: str = ""
    document_ids: list[str] = Field(default_factory=list)
    consistency_findings: list[CrossDocFinding] = Field(default_factory=list)
    responsibility_findings: list[CrossDocFinding] = Field(default_factory=list)
    topics_checked: list[str] = Field(default_factory=list)  # 已核查主题，明示边界(Q7)


# 解析 ReviewResult 里对后面才定义的模型的前向引用。
ReviewResult.model_rebuild()

