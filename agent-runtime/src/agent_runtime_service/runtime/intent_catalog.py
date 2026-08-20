"""发布可引用的确定性意图目录，避免 Planner 每次临时发明业务意图。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime_service.runtime.models import IntentResult


@dataclass(frozen=True)
class IntentDefinition:
    """一个领域/动作/槽位意图的最小声明，风险与工具仍由发布快照最终约束。"""

    name: str
    domain: str
    action: str
    examples: tuple[str, ...]
    required_entities: tuple[str, ...] = ()


class IntentCatalog:
    """启动期加载、请求期只读的意图目录；规则命中优先于可选语义模型。"""

    def __init__(self, version: str, definitions: tuple[IntentDefinition, ...]) -> None:
        """校验唯一意图名与版本，避免同一请求得到不可解释的多个权威规则。"""
        if not version.strip() or len({item.name for item in definitions}) != len(definitions):
            raise ValueError("intent catalog requires a version and unique intent names")
        self.version = version
        self._definitions = definitions

    @property
    def definitions(self) -> tuple[IntentDefinition, ...]:
        """返回不可变规则元组，供受治理语义分析构造受限输出 Schema。"""
        return self._definitions

    def resolve(self, text: str) -> IntentResult:
        """按例词命中数和声明顺序给出稳定候选；无命中显式降为通用意图。"""
        lowered = text.lower()
        matches: list[tuple[int, IntentDefinition]] = []
        for definition in self._definitions:
            count = sum(1 for example in definition.examples if example.lower() in lowered)
            if count:
                matches.append((count, definition))
        if not matches:
            return IntentResult(
                name="general_question",
                confidence=0.62,
                reason=f"No deterministic rule matched in catalog {self.version}.",
            )
        score, selected = max(matches, key=lambda item: item[0])
        return IntentResult(
            name=selected.name,
            confidence=min(0.95, 0.78 + 0.08 * score),
            reason=(
                f"Catalog {self.version} matched {selected.domain}/{selected.action}; "
                f"required_entities={','.join(selected.required_entities) or 'none'}."
            ),
        )

    @classmethod
    def from_snapshot(cls, raw: dict[str, object]) -> IntentCatalog:
        """从已编译 Snapshot 恢复目录；拒绝未经 Compiler 验证的松散运行时数据。"""
        version = raw.get("version")
        definitions = raw.get("definitions")
        if not isinstance(version, str) or not isinstance(definitions, list):
            raise ValueError("compiled intent catalog is malformed")
        parsed: list[IntentDefinition] = []
        for item in definitions:
            if not isinstance(item, dict):
                raise ValueError("compiled intent definition is malformed")
            name = item.get("name")
            domain = item.get("domain")
            action = item.get("action")
            examples = item.get("examples")
            entities = item.get("required_entities", [])
            if (
                not isinstance(name, str)
                or not isinstance(domain, str)
                or not isinstance(action, str)
                or not isinstance(examples, list)
                or not isinstance(entities, list)
                or not all(isinstance(value, str) for value in examples + entities)
            ):
                raise ValueError("compiled intent definition has invalid field types")
            parsed.append(IntentDefinition(name, domain, action, tuple(examples), tuple(entities)))
        return cls(version, tuple(parsed))


DEFAULT_INTENT_CATALOG = IntentCatalog(
    "platform-default/v1",
    (
        IntentDefinition("refund_application", "order", "refund", ("refund", "退款", "退货")),
        IntentDefinition(
            "compliance_review", "governance", "review", ("audit", "review", "审查", "审核", "合规")
        ),
        IntentDefinition(
            "tool_operation",
            "operation",
            "mutate",
            ("create", "update", "delete", "execute", "创建", "更新", "执行"),
        ),
        IntentDefinition(
            "knowledge_query",
            "knowledge",
            "search",
            ("find", "search", "query", "查询", "检索", "查找"),
        ),
    ),
)


def supported_catalog_versions() -> tuple[str, ...]:
    """公开当前 Runtime 实际装载的目录版本，发布端可据此拒绝无法执行的声明。"""
    return (DEFAULT_INTENT_CATALOG.version,)


def resolve_catalog(compiled_plan: dict[str, object]) -> IntentCatalog:
    """返回发布绑定目录或内置基线，版本漂移和缺失规则均在模型调用前失败。"""
    requested = str(compiled_plan.get("intent_catalog_version", DEFAULT_INTENT_CATALOG.version))
    raw_catalog = compiled_plan.get("intent_catalog")
    if raw_catalog is None:
        if requested != DEFAULT_INTENT_CATALOG.version:
            raise RuntimeError(
                "published intent catalog is not deployed on this Runtime: " + requested
            )
        return DEFAULT_INTENT_CATALOG
    if not isinstance(raw_catalog, dict):
        raise RuntimeError("published intent catalog is malformed")
    catalog = IntentCatalog.from_snapshot(raw_catalog)
    if catalog.version != requested:
        raise RuntimeError("published intent catalog version does not match compiled plan")
    return catalog
