"""GMP 语义判定；所有请求统一经过 llm-gateway。

覆盖率、数据可靠性的判定从关键词基线升级为大模型语义判定，走 llm-gateway
(不直连厂商)。prompt 直接移植自 Java 版经真实文件验证的 systemPrompt：
- 覆盖率：CoverageReviewService.systemPrompt / userPrompt
- 数据可靠性：DataIntegrityService.systemPrompt

响应先经过 Pydantic 校验。异常向上抛，由审查服务标记 UNCERTAIN，禁止静默
把关键词结果伪装成正式语义判定。
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import AlcoaRisk, ChecklistItem, DataIntegrityFieldCheck
from app.infrastructure.llm_gateway_client import LlmGatewayClient


class CoverageJudgeItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    requirement_id: str = Field(alias="requirementId")
    status: Literal["COVERED", "PARTIAL", "MISSING", "NOT_APPLICABLE", "UNCERTAIN"]
    evidence: str = ""
    missing_fields: list[str] = Field(default_factory=list, alias="missingFields")
    reason: str = ""


class CoverageJudgeBatch(BaseModel):
    results: list[CoverageJudgeItem]


class LlmJudge:
    def __init__(self, gateway: LlmGatewayClient, model: str) -> None:
        self.gateway = gateway
        self.model = model

    def _complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        return self.gateway.complete_json(self.model, system_prompt, user_prompt)

    # --- 覆盖率判定 (移植自 CoverageReviewService) ---

    def judge_coverage(self, item: ChecklistItem, evidence: list[str]) -> dict:
        """判定单条要求的覆盖状态。返回 {status, evidence, missingFields, reason}。"""
        raw = self._complete_json(
            self._coverage_system_prompt(),
            self._coverage_user_prompt(item, evidence),
        )
        raw.setdefault("requirementId", item.requirement_id)
        return CoverageJudgeItem.model_validate(raw).model_dump(by_alias=True)

    def judge_coverage_batch(
        self,
        cases: list[tuple[ChecklistItem, list[str]]],
    ) -> dict[str, dict]:
        """一次判定多条要求，减少网关调用并保持同批结论口径一致。"""
        raw = self._complete_json(
            self._coverage_batch_system_prompt(),
            self._coverage_batch_user_prompt(cases),
        )
        parsed = CoverageJudgeBatch.model_validate(raw)
        return {
            item.requirement_id: item.model_dump(by_alias=True)
            for item in parsed.results
        }

    @staticmethod
    def _coverage_system_prompt() -> str:
        return (
            "你是资深 GMP 质量体系审查员。任务：判断“企业文件片段”是否覆盖了给定的“法规强制要求”。\n"
            "只输出 JSON，不要 markdown、不要额外文字。JSON 格式：\n"
            "{\n"
            '  "status": "COVERED | PARTIAL | MISSING | NOT_APPLICABLE | UNCERTAIN",\n'
            '  "evidence": "企业文件中支持该结论的原文摘录，没有则空字符串",\n'
            '  "missingFields": ["缺少的必填字段或要素"],\n'
            '  "reason": "一句话判定理由"\n'
            "}\n"
            "判定标准：\n"
            "- COVERED：企业文件明确、完整地规定了该要求(含关键要素)。\n"
            "- PARTIAL：提到了但不完整，或缺少部分必填字段/关键要素。\n"
            "- MISSING：企业文件片段中找不到与该要求相关的内容。\n"
            "- NOT_APPLICABLE：根据文件类型和内容，该要求明确不适用。\n"
            "- UNCERTAIN：证据不足、片段冲突或无法可靠判断。\n"
            "注意：证据不足时必须判 UNCERTAIN，不要臆测企业“应该有”。"
        )

    @staticmethod
    def _coverage_user_prompt(item: ChecklistItem, evidence: list[str]) -> str:
        evidence_text = "\n---\n".join(evidence) if evidence else "(未检索到相关片段)"
        fields = "、".join(item.required_fields)
        source = "；".join(item.regulation_refs)
        return (
            "【法规强制要求】\n"
            f"模块: {item.module}\n"
            f"要求: {item.description}\n"
            f"必填字段/关键要素: {fields}\n"
            f"来源: {source}\n\n"
            "【从企业文件中检索到的相关片段】\n"
            f"{evidence_text}\n\n"
            "请判定该要求在企业文件中的覆盖状态，只输出 JSON。"
        )

    @staticmethod
    def _coverage_batch_system_prompt() -> str:
        return (
            "你是资深 GMP 质量体系审查员。一次核查多条法规要求。只输出 JSON，格式：\n"
            '{"results":[{"requirementId":"REQ-XXX","status":"COVERED|PARTIAL|MISSING|NOT_APPLICABLE|UNCERTAIN",'
            '"evidence":"企业文件原文","missingFields":[],"reason":"判定理由"}]}\n'
            "每个 requirementId 必须且只能返回一次。证据必须来自给定企业片段；证据不足用 UNCERTAIN。"
            "只有明确不适用于该文件时才用 NOT_APPLICABLE。"
        )

    @staticmethod
    def _coverage_batch_user_prompt(cases: list[tuple[ChecklistItem, list[str]]]) -> str:
        sections: list[str] = []
        for item, evidence in cases:
            snippets = "\n---\n".join(evidence) if evidence else "(未检索到企业证据)"
            sections.append(
                f"【{item.requirement_id}】模块:{item.module}\n要求:{item.description}\n"
                f"关键要素:{'、'.join(item.required_fields)}\n来源:{'；'.join(item.regulation_refs)}\n"
                f"企业片段:\n{snippets}"
            )
        return "\n\n========\n\n".join(sections)

    # --- 数据可靠性判定 (移植自 DataIntegrityService，一次调用判所有项) ---

    def judge_data_integrity(
        self,
        content: str,
        fields: list[DataIntegrityFieldCheck],
        risks: list[AlcoaRisk],
    ) -> dict:
        """一次性判定所有字段是否齐全 + 所有 ALCOA+ 风险。返回 {fields:[...], risks:[...]}。"""
        return self._complete_json(
            self._di_system_prompt(fields, risks),
            "待核查记录/文件内容：\n\n" + content,
        )

    @staticmethod
    def _di_system_prompt(fields: list[DataIntegrityFieldCheck], risks: list[AlcoaRisk]) -> str:
        field_list = "".join(
            f"- {f.id} 「{f.field}」：{f.requirement}\n" for f in fields
        )
        risk_list = "".join(
            f"- {r.id} 「{r.risk}」({r.principle})：{r.requirement}\n" for r in risks
        )
        return (
            "你是资深 GMP 数据完整性(ALCOA+)审查员。请核查给定的记录/文件，完成两项任务：\n\n"
            "【任务一：关键字段是否齐全】逐项判断以下字段在文件中是否出现：\n"
            f"{field_list}\n"
            "【任务二：ALCOA+ 风险排查】逐项判断以下风险是否存在：\n"
            f"{risk_list}\n"
            "只输出 JSON，不要 markdown、不要额外文字。格式：\n"
            "{\n"
            '  "fields": [ {"id":"FIELD-XXX","present":"PRESENT|MISSING","evidence":"原文摘录，无则空","comment":"简短说明"} ],\n'
            '  "risks": [ {"id":"RISK-XXX","verdict":"OK|RISK|UNCLEAR","evidence":"原文摘录，无则空","comment":"简短说明或整改建议"} ]\n'
            "}\n"
            "判定说明：\n"
            "- 字段 present：PRESENT=文件明确出现该字段/要求；MISSING=找不到。\n"
            "- 风险 verdict：OK=文件明确有合规做法；RISK=文件出现明显硬伤(如允许事后补记、涂改)；UNCLEAR=文件未提及，无法判断。\n"
            "- 证据必须来自原文，不要编造。不确定时用 UNCLEAR/MISSING，不要臆测。"
        )

    # --- 跨文档断言抽取 (组件⑥;见 gmp-cross-document-plan) ---
    # 重构说明：不再让模型"判矛盾"(易串味/重复/不确定)。改为让模型只做它擅长的
    # "文本→结构化断言"抽取；矛盾判定交给下游纯规则比对(确定、可复现、可解释)。

    def extract_assertions(self, filename: str, text: str, attribute_hints: list[str]) -> dict:
        """从单份文件抽取合规断言三元组，不跨文件、不判矛盾。

        返回 {assertions:[{object, attribute, value, quote}]}。
        object=受控对象(如"灌装间"),attribute=属性(如"洁净度等级"),
        value=取值(如"D级"),quote=原文摘录。下游按(object,attribute)分组比对。
        """
        return self._complete_json(
            self._assertion_system_prompt(attribute_hints),
            f"文件名：{filename}\n\n文件内容：\n{text}\n\n请抽取合规断言，只输出 JSON。",
        )

    @staticmethod
    def _assertion_system_prompt(attribute_hints: list[str]) -> str:
        hints = "、".join(attribute_hints) if attribute_hints else "洁净度等级、检验频次、温湿度限度等"
        return (
            "你是资深 GMP 质量体系审查员。任务：从单份企业文件中抽取"
            "【合规断言】——即“某个受控对象的某个属性被规定为某个值”。\n"
            "只输出 JSON，不要 markdown、不要额外文字。格式：\n"
            "{\n"
            '  "assertions": [ {"object":"受控对象","attribute":"属性","value":"取值","quote":"原文摘录"} ]\n'
            "}\n"
            "抽取规则(务必遵守)：\n"
            f"- 重点关注这些属性(但不限于)：{hints}。\n"
            "- object 用规范、可跨文件对齐的名称：同一处所/对象在不同文件里要抽成【同一个】"
            "object 名(例：'无菌灌装间''灌装间'都归一为'灌装间';'检验室''QC实验室'归一为'QC实验室')。\n"
            "- attribute 用规范属性名(如'洁净度等级''检验频次''温度范围''相对湿度范围')，"
            "同一属性不同说法归一(如'环境控制级别'归一为'洁净度等级')。\n"
            "- value 保留可比较的具体取值(如'D级''每月一次''18~26℃')。\n"
            "- quote 必须来自原文，不得编造。\n"
            "- 只抽有明确取值的断言；泛泛描述、无具体值的不抽。"
        )

    # --- 跨文档职责判定 (仅对规则粗筛判不了的边界情况兜底) ---

    def judge_responsibility(self, snippets: list[dict]) -> dict:
        """对职责分离做语义兜底判断。返回 {issues:[{type,role,detail}], reason}。

        注意：主判定走规则(职责矩阵求交)，此方法仅兜底规则识别不了的表述。
        """
        return self._complete_json(
            self._responsibility_system_prompt(),
            "各文件中与岗位职责相关的片段：\n\n"
            + "\n---\n".join(f"《{s.get('filename','?')}》：{s.get('text','')}" for s in snippets),
        )

    @staticmethod
    def _responsibility_system_prompt() -> str:
        return (
            "你是资深 GMP 质量体系审查员。任务：判断岗位职责是否存在【职责分离】问题——"
            "同一个人既执行操作又负责复核/批准(既当运动员又当裁判)。\n"
            "只输出 JSON，不要 markdown、不要额外文字。格式：\n"
            "{\n"
            '  "issues": [ {"type":"职责分离冲突|职责空缺|职责重叠","role":"涉及的人或岗位","detail":"说明"} ],\n'
            '  "reason": "总体判定理由"\n'
            "}\n"
            "判定规则：\n"
            "- 只报【明确】的问题；同名可能是不同人、不同产品线的同名岗位属正常，拿不准就不报。\n"
            "- 证据必须来自原文。issues 为空表示未发现明确问题。"
        )
