"""Compile immutable Control Plane releases into Runtime executable plans.

Compilation is the compatibility boundary between authoring and execution:
Runtime accepts an explicit plan, never a mutable draft or loosely interpreted
release document.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from typing import Any

from jsonschema import Draft202012Validator
from platform_sdk.contracts.capabilities import required_runtime_capabilities
from platform_sdk.contracts.execution_profile import (
    ExecutionProfileError,
    resolve_execution_profile,
)
from platform_sdk.contracts.runtime_snapshot import compile_intent_catalog
from platform_sdk.contracts.workflow import WorkflowConditionError, compile_workflow_condition
from pydantic import BaseModel, Field

from agent_runtime_service.runtime.models import (
    ExecutionLifecycle,
    ExecutionRequirements,
    ReasoningMode,
)

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


class SnapshotCompileError(ValueError):
    """Published snapshot cannot be executed without silently ignoring configuration."""


class CompiledAgentPlan(BaseModel):
    contract_version: str = "1.0"
    contract_hash: str
    graph_id: str
    graph_entrypoint: str
    graph_terminal_nodes: list[str]
    graph_execution_order: list[str]
    graph_node_kinds: dict[str, str]
    executor_profile: str
    execution_requirements: ExecutionRequirements = Field(default_factory=ExecutionRequirements)
    intent_catalog_version: str = "platform-default/v1"
    intent_catalog: dict[str, Any] | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    workflow_policy: dict[str, Any] = Field(default_factory=dict)
    prompt_template: str
    prompt_variables: list[str] = Field(default_factory=list)
    prompt_output_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    logical_model: str
    fallback_models: list[str] = Field(default_factory=list)
    data_region: str | None = None
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)
    subagents: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def retrieval_top_k(self) -> int:
        """返回发布知识绑定中最大的候选数，用于兼容未提供有效策略的旧快照。"""
        return max((int(item.get("top_k", 8)) for item in self.knowledge), default=8)

    @property
    def knowledge_filters(self) -> dict[str, Any]:
        """按知识库汇总发布过滤条件；过滤条件属于快照契约而非模型建议。"""
        return {str(item["knowledge_base"]): item.get("filters", {}) for item in self.knowledge}

    @property
    def knowledge_contracts(self) -> dict[str, dict[str, str]]:
        """返回每个知识绑定的索引与嵌入契约，供 RAG 请求固定读取版本。"""
        return {
            str(item["knowledge_base"]): {
                "index_version": str(item.get("index_version", "")),
                "embedding_contract_id": str(item.get("embedding_contract_id", "")),
                "retrieval_evaluation_id": str(item.get("retrieval_evaluation_id", "")),
            }
            for item in self.knowledge
        }


def compile_snapshot(
    snapshot: dict[str, Any],
    *,
    tenant_id: str,
    agent_id: str,
    fallback_model: str,
) -> CompiledAgentPlan:
    """将不可变发布快照编译成 Runtime 唯一接受的执行契约。

    编译同时校验租户/Agent 身份、图可达性、Prompt 变量、模型路由与输出 Schema，
    防止 Runtime 在执行时“猜测”草稿含义。空快照仅走明确标识的本地开发计划。
    """
    if not snapshot:
        return _local_plan(fallback_model)
    if snapshot.get("schema_version") != "1.0":
        raise SnapshotCompileError("unsupported published snapshot schema_version")
    if snapshot.get("tenant_id") != tenant_id or snapshot.get("agent_id") != agent_id:
        raise SnapshotCompileError("published snapshot identity does not match the run")

    spec = snapshot.get("spec")
    if not isinstance(spec, dict):
        raise SnapshotCompileError("published snapshot spec is missing")
    graph = _mapping(spec, "graph")
    nodes = graph.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        raise SnapshotCompileError("published graph has no nodes")
    node_kinds = {
        str(_required(item, "node_id")): str(_required(item, "kind"))
        for item in nodes
        if isinstance(item, dict)
    }
    if len(node_kinds) != len(nodes):
        raise SnapshotCompileError("published graph node_id values must be unique")
    entrypoint = str(_required(graph, "entrypoint"))
    terminals = [str(item) for item in graph.get("terminal_nodes") or []]
    if entrypoint not in node_kinds or not terminals or not set(terminals) <= node_kinds.keys():
        raise SnapshotCompileError("published graph entrypoint or terminal nodes are invalid")
    edges = graph.get("edges") or []
    order = _reachable_order(entrypoint, edges, node_kinds)
    unreachable = sorted(set(node_kinds) - set(order))
    if unreachable:
        raise SnapshotCompileError(f"published graph contains unreachable nodes: {unreachable}")
    if not set(order).intersection(terminals):
        raise SnapshotCompileError("published graph has no reachable terminal node")
    workflow_policy = _compile_workflow_policy(entrypoint, terminals, edges, node_kinds)

    prompt = _mapping(spec, "prompt")
    template = str(_required(prompt, "system_template"))
    declared = [str(item) for item in prompt.get("variables") or []]
    used = set(_VARIABLE.findall(template))
    if used - set(declared):
        raise SnapshotCompileError("published prompt contains undeclared variables")
    output_schema = prompt.get("output_schema")
    if output_schema is not None:
        Draft202012Validator.check_schema(output_schema)

    policy = _mapping(spec, "model_policy")
    routes = {
        str(item.get("route_name")): item
        for item in policy.get("routes") or []
        if isinstance(item, dict) and item.get("route_name")
    }
    default_route = routes.get(str(policy.get("default_route")))
    if not default_route or not default_route.get("models"):
        raise SnapshotCompileError("published model policy has no executable default route")
    models = [str(item) for item in default_route["models"]]
    fallback_route = routes.get(str(default_route.get("fallback_route")))
    fallback_models = models[1:]
    if fallback_route:
        fallback_models.extend(str(item) for item in fallback_route.get("models") or [])

    execution_requirements, executor_profile = _compile_execution_requirements(spec)
    try:
        intent_catalog = compile_intent_catalog(spec)
    except ValueError as exc:
        raise SnapshotCompileError(str(exc)) from exc
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CompiledAgentPlan(
        contract_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        graph_id=str(_required(graph, "graph_id")),
        graph_entrypoint=entrypoint,
        graph_terminal_nodes=terminals,
        graph_execution_order=order,
        graph_node_kinds=node_kinds,
        executor_profile=executor_profile,
        execution_requirements=execution_requirements,
        intent_catalog_version=str(spec.get("intent_catalog_version", "platform-default/v1")),
        intent_catalog=intent_catalog,
        required_capabilities=required_runtime_capabilities(spec),
        workflow_policy=workflow_policy,
        prompt_template=template,
        prompt_variables=declared,
        prompt_output_schema=output_schema,
        tools=[dict(item) for item in spec.get("tools") or []],
        knowledge=[dict(item) for item in spec.get("knowledge") or []],
        logical_model=models[0],
        fallback_models=list(dict.fromkeys(fallback_models)),
        data_region=default_route.get("data_region"),
        retrieval_policy=dict(spec.get("retrieval_policy") or {}),
        subagents=[dict(item) for item in spec.get("subagents") or []],
    )


def render_prompt(plan: dict[str, Any], variables: dict[str, Any]) -> str:
    """以声明变量渲染发布 Prompt；任何缺失变量都会拒绝执行而非静默留空。"""
    template = str(plan.get("prompt_template", ""))
    used = set(_VARIABLE.findall(template))
    missing = sorted(name for name in used if _resolve(variables, name) is None)
    if missing:
        raise SnapshotCompileError(f"published prompt variables are missing: {missing}")

    def replace(match: re.Match[str]) -> str:
        """解析单个 ``{{path}}`` 占位符；外层已验证缺失值，故不做隐式默认。"""
        value = _resolve(variables, match.group(1))
        return str(value) if value is not None else ""

    return _VARIABLE.sub(replace, template)


def validate_tool_manifests(plan: dict[str, Any], manifests: list[dict[str, Any]]) -> None:
    """比对目录实况与发布工具绑定，阻止版本、风险或审批策略漂移。"""
    actual = {(str(item.get("name")), str(item.get("version"))): item for item in manifests}
    for binding in plan.get("tools", []):
        key = (str(binding.get("tool_name")), str(binding.get("version")))
        manifest = actual.get(key)
        if manifest is None:
            raise SnapshotCompileError(f"published tool is unavailable: {key[0]}:{key[1]}")
        for published_name, manifest_name in (
            ("risk", "risk"),
            ("approval_required", "approval_required"),
            ("timeout_seconds", "timeout_seconds"),
        ):
            if binding.get(published_name) != manifest.get(manifest_name):
                raise SnapshotCompileError(
                    f"published tool policy drift for {key[0]}:{key[1]} field {published_name}"
                )


def validate_final_output(plan: dict[str, Any], answer: str) -> None:
    """当发布 Prompt 声明输出 Schema 时，验证最终回答是符合约束的 JSON。"""
    schema = plan.get("prompt_output_schema")
    if not schema:
        return
    try:
        value = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise SnapshotCompileError(
            "final answer must be JSON because the published prompt declares output_schema"
        ) from exc
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise SnapshotCompileError(
            f"final answer violates published output_schema: {errors[0].message}"
        )


def _reachable_order(entrypoint: str, edges: list[Any], node_kinds: dict[str, str]) -> list[str]:
    """广度遍历发布图并拒绝未知边端点，供编译器发现不可达节点。"""
    adjacency: dict[str, list[str]] = {name: [] for name in node_kinds}
    for edge in edges:
        if not isinstance(edge, dict):
            raise SnapshotCompileError("published graph edge must be an object")
        source, target = str(edge.get("from_node")), str(edge.get("to_node"))
        if source not in adjacency or target not in adjacency:
            raise SnapshotCompileError("published graph edge references an unknown node")
        adjacency[source].append(target)
    queue, seen, order = deque([entrypoint]), set(), []
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        queue.extend(adjacency[current])
    return order


def _compile_workflow_policy(
    entrypoint: str,
    terminals: list[str],
    edges: list[Any],
    node_kinds: dict[str, str],
) -> dict[str, Any]:
    """将发布 Graph DSL 变为 Runtime 可执行的受限迁移策略。

    这里故意不把租户定义的节点映射为任意 Python 回调。Runtime 只有固定的
    ``decision/retrieval/tool/answer/clarify`` 安全节点；发布图只能声明它们的
    可达顺序，因而既能改变业务编排，又不会让快照取得代码执行或流程绕过权限。
    """
    normalized = {
        node_id: _workflow_role(node_id, kind, terminal=node_id in terminals)
        for node_id, kind in node_kinds.items()
    }
    adjacency: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_kinds}
    for edge in edges:
        if not isinstance(edge, dict):
            raise SnapshotCompileError("published graph edge must be an object")
        source, target = str(edge.get("from_node")), str(edge.get("to_node"))
        try:
            condition = compile_workflow_condition(edge.get("condition"))
        except WorkflowConditionError as exc:
            raise SnapshotCompileError(f"published graph condition is invalid: {exc}") from exc
        adjacency[source].append(
            {"to": target, "condition": condition.model_dump(mode="json") if condition else None}
        )

    for terminal in terminals:
        if normalized[terminal] not in {"answer", "clarify"}:
            raise SnapshotCompileError("published terminal node must be answer or clarify")
        if adjacency[terminal]:
            raise SnapshotCompileError("published terminal node cannot have outgoing edges")

    allowed_targets = {
        "decision": {"retrieval", "tool", "answer", "clarify"},
        "retrieval": {"decision", "answer", "clarify"},
        "tool": {"decision", "answer", "clarify"},
        "answer": set(),
        "clarify": set(),
    }
    for node_id, role in normalized.items():
        targets = [normalized[item["to"]] for item in adjacency[node_id]]
        if any(target not in allowed_targets[role] for target in targets):
            raise SnapshotCompileError(
                f"published workflow transition is not allowed from {role} at {node_id}"
            )
        if role in {"retrieval", "tool"} and len(targets) != 1:
            raise SnapshotCompileError(
                f"published {role} node must have exactly one controlled successor"
            )
        if role == "decision" and not targets:
            raise SnapshotCompileError("published decision node must declare at least one action")
        if len(targets) > 1 and not all(item["condition"] for item in adjacency[node_id]):
            raise SnapshotCompileError(
                f"published branching node requires conditions on every edge: {node_id}"
            )

    entry_role = normalized[entrypoint]
    if entry_role not in {"decision", "retrieval", "clarify"}:
        raise SnapshotCompileError("published workflow entry must be decision, retrieval or clarify")
    return {
        "version": "workflow-policy/v1",
        "entrypoint": entrypoint,
        "terminals": terminals,
        "node_roles": normalized,
        "adjacency": adjacency,
    }


def _workflow_role(node_id: str, kind: str, *, terminal: bool) -> str:
    """归一发布节点类型；兼容旧 ``llm``，但拒绝无执行语义的任意种类。"""
    normalized = kind.strip().lower()
    if normalized in {"decision", "planner"}:
        return "decision"
    if normalized in {"retrieval", "rag"}:
        return "retrieval"
    if normalized in {"tool", "action"}:
        return "tool"
    if normalized in {"answer", "final"}:
        return "answer"
    if normalized in {"clarify", "clarification"}:
        return "clarify"
    if normalized == "llm":
        return "answer" if terminal else "decision"
    raise SnapshotCompileError(f"published graph node kind is unsupported: {node_id}:{kind}")


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
    """取得必须为对象的快照分段；不接受宽松类型转换以免隐藏发布错误。"""
    item = value.get(name)
    if not isinstance(item, dict):
        raise SnapshotCompileError(f"published snapshot {name} is missing")
    return item


def _compile_execution_requirements(spec: dict[str, Any]) -> tuple[ExecutionRequirements, str]:
    """把新双轴声明或旧 Profile 编译为唯一部署别名，避免运行时猜测组合语义。"""
    try:
        shared, profile = resolve_execution_profile(spec)
    except ExecutionProfileError as exc:
        raise SnapshotCompileError(str(exc)) from exc
    return (
        ExecutionRequirements(
            lifecycle=ExecutionLifecycle(shared.lifecycle),
            reasoning=ReasoningMode(shared.reasoning),
        ),
        profile,
    )


def _required(value: dict[str, Any], name: str) -> Any:
    """读取非空必填字段，并把缺失转成可定位的编译错误。"""
    item = value.get(name)
    if item is None or item == "":
        raise SnapshotCompileError(f"published snapshot field is required: {name}")
    return item


def _resolve(values: dict[str, Any], path: str) -> Any:
    """安全解析点分变量路径；中途不存在返回 None 交由渲染器统一拒绝。"""
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _local_plan(model: str) -> CompiledAgentPlan:
    """构造显式标记为未版本化的本地开发计划，不能伪装成生产发布。"""
    return CompiledAgentPlan(
        contract_hash="local-unversioned",
        graph_id="runtime-default",
        graph_entrypoint="agent-loop",
        graph_terminal_nodes=["agent-loop"],
        graph_execution_order=["agent-loop"],
        graph_node_kinds={"agent-loop": "agent"},
        executor_profile="local-default/v1",
        execution_requirements=ExecutionRequirements(),
        intent_catalog_version="local-development/v1",
        required_capabilities=[],
        workflow_policy={
            "version": "workflow-policy/v1",
            "entrypoint": "agent-loop",
            "terminals": ["agent-loop"],
            "node_roles": {"agent-loop": "decision"},
            "adjacency": {"agent-loop": []},
            "local_development_only": True,
        },
        prompt_template="",
        logical_model=model,
    )
