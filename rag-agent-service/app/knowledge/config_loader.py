"""读取 gmp-config 下的 3 个 JSON 配置，转成领域模型。

这些配置是从 llm-gateway(Java 版)移植过来的经真实文件验证的资产：
- requirement-checklist.json：13 条 GMP 强制要求清单
- document-type-mapping.json：正大天晴两级分类映射(文件类型 → 应查清单)
- data-integrity-checklist.json：ALCOA+ 字段检查 + 风险排查

配置改动只需改 JSON，不用动代码。
"""
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from app.domain.models import (
    AlcoaRisk,
    ChecklistItem,
    DataIntegrityFieldCheck,
    severity_from_zh,
)

CONFIG_DIR = Path(__file__).parent / "config"

log = logging.getLogger(__name__)


def _load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache
def load_checklist() -> list[ChecklistItem]:
    """13 条要求清单。JSON 的 requirement 存进 description，module 兼作 title。"""
    data = _load_json("requirement-checklist.json")
    items: list[ChecklistItem] = []
    for raw in data.get("items", []):
        items.append(
            ChecklistItem(
                requirement_id=raw["id"],
                module=raw["module"],
                dimension=raw["dimension"],
                title=raw["module"],
                description=raw["requirement"],
                severity=severity_from_zh(raw.get("severity")),
                required_fields=raw.get("requiredFields", []),
                regulation_refs=[raw["source"]] if raw.get("source") else [],
                keywords=raw.get("keywords", []),
                reviewer=raw.get("reviewer", "coverage"),
                level=raw.get("level", "common"),
                applicable_document_types=raw.get("applicableDocumentTypes", []),
                applicability=raw.get("applicability", ""),
                synonyms=raw.get("synonyms", []),
                positive_examples=raw.get("positiveExamples", []),
                negative_examples=raw.get("negativeExamples", []),
            )
        )
    return items


@lru_cache
def load_type_mapping() -> dict:
    """返回 {modules, typeToRequirements} 原始结构，供分类过滤和分类树接口使用。"""
    data = _load_json("document-type-mapping.json")
    return {
        "modules": data.get("modules", {}),
        "type_to_requirements": data.get("typeToRequirements", {}),
    }


def _normalize_document_type(document_type: str) -> str:
    """归一化二级分类名,消除静默落 default 的常见诱因。

    前端可能传"清洁验证管理程序""清洁验证管理.docx""清洁验证管理 "等变体,
    而映射 key 是"清洁验证管理"。去掉扩展名、尾部"程序"二字、首尾空白后再匹配。
    """
    name = document_type.strip()
    name = re.sub(r"\.(docx?|pdf|txt|md)$", "", name, flags=re.IGNORECASE)
    name = name.strip()
    if name.endswith("程序"):
        name = name[:-2]
    return name


def requirement_ids_for(document_type: str | None) -> list[str]:
    """按二级分类返回应核查的清单条目 ID；未命中走 _default 兜底。

    匹配前先归一化 document_type(去扩展名/尾部"程序"/空白),命中则用映射;
    最终落 _default 时打 warning,让"为什么只查了兜底两条"可诊断、不静默。
    """
    return list(resolve_requirements(document_type)["requirement_ids"])


def resolve_requirements(document_type: str | None) -> dict:
    """返回清单选择结果及可展示的映射诊断信息。"""
    mapping = load_type_mapping()["type_to_requirements"]
    if document_type:
        if document_type in mapping:
            return {
                "requirement_ids": list(mapping[document_type]),
                "status": "MATCHED",
                "normalized_type": document_type,
                "warning": None,
            }
        normalized = _normalize_document_type(document_type)
        if normalized in mapping:
            return {
                "requirement_ids": list(mapping[normalized]),
                "status": "NORMALIZED_MATCH",
                "normalized_type": normalized,
                "warning": None,
            }
    warning = f"文件类型 {document_type!r} 未命中映射，仅执行通用审查，结果不代表完整条款覆盖率。"
    log.warning(warning)
    return {
        "requirement_ids": list(mapping.get("_default", [])),
        "status": "DEFAULT_ONLY",
        "normalized_type": _normalize_document_type(document_type) if document_type else None,
        "warning": warning,
    }


def mapping_diagnostics() -> dict:
    data = load_type_mapping()
    all_types = {item for values in data["modules"].values() for item in values}
    mapped = {key for key in data["type_to_requirements"] if key != "_default"}
    missing = sorted(all_types - mapped)
    return {
        "total_document_types": len(all_types),
        "mapped_document_types": len(all_types & mapped),
        "unmapped_document_types": len(missing),
        "unmapped_types": missing,
    }


def validate_knowledge_config() -> None:
    """启动时校验清单身份、审查器归属和映射引用，避免配置错误静默生效。"""
    items = load_checklist()
    ids = [item.requirement_id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("requirement-checklist.json 存在重复 id")
    allowed_reviewers = {"coverage", "data_integrity", "cross_document", "clarity", "standard_floor"}
    invalid_reviewers = sorted({item.reviewer for item in items} - allowed_reviewers)
    if invalid_reviewers:
        raise ValueError(f"清单包含未知 reviewer: {invalid_reviewers}")
    mapping = load_type_mapping()["type_to_requirements"]
    referenced = {req_id for values in mapping.values() for req_id in values}
    unknown_ids = sorted(referenced - set(ids))
    if unknown_ids:
        raise ValueError(f"文档类型映射引用了不存在的清单 id: {unknown_ids}")


@lru_cache
def load_field_checks() -> list[DataIntegrityFieldCheck]:
    data = _load_json("data-integrity-checklist.json")
    return [
        DataIntegrityFieldCheck(
            id=raw["id"],
            field=raw["field"],
            requirement=raw["requirement"],
            keywords=raw.get("keywords", []),
            severity=severity_from_zh(raw.get("severity")),
        )
        for raw in data.get("fieldChecks", [])
    ]


@lru_cache
def load_alcoa_risks() -> list[AlcoaRisk]:
    data = _load_json("data-integrity-checklist.json")
    return [
        AlcoaRisk(
            id=raw["id"],
            principle=raw["principle"],
            risk=raw["risk"],
            requirement=raw["requirement"],
            red_flags=raw.get("redFlags", []),
            expect=raw.get("expect", []),
            severity=severity_from_zh(raw.get("severity")),
        )
        for raw in data.get("alcoaRisks", [])
    ]


@lru_cache
def load_vague_words() -> list[dict]:
    """模糊词列表：[{word, suggestion, severity}]。"""
    return _load_json("clarity-config.json").get("vagueWords", [])


@lru_cache
def load_term_groups() -> list[dict]:
    """术语统一组：[{canonical, variants, note}]。"""
    return _load_json("clarity-config.json").get("termGroups", [])


def config_version() -> str:
    return _load_json("requirement-checklist.json").get("version", "unknown")


@lru_cache
def load_numeric_topics() -> list[dict]:
    """数值/标准型跨文档主题：[{topic, aliases, unit_hint, sameScenarioNote}]。"""
    return _load_json("cross-document-topics.json").get("numericTopics", [])


@lru_cache
def load_responsibility_topics() -> list[dict]:
    """职责型跨文档主题：[{topic, aliases, actions, separationRule}]。"""
    return _load_json("cross-document-topics.json").get("responsibilityTopics", [])


@lru_cache
def load_regulation_floors() -> list[dict]:
    """法规底线表(3.2a)：[{attribute, aliases, direction, floor, unit, scope, source}]。"""
    return _load_json("regulation-floor.json").get("floors", [])


@lru_cache
def load_grade_orders() -> dict:
    """等级序表(3.2a 等级型属性用)：{属性名: [由严到宽的等级]}，如洁净度 A>B>C>D。"""
    return _load_json("regulation-floor.json").get("gradeOrders", {})
