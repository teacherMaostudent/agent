"""发布快照到 Runtime 可执行计划的共享确定性编译契约。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from platform_sdk.contracts.capabilities import required_runtime_capabilities
from platform_sdk.contracts.workflow import (
    WorkflowConditionError,
    compile_workflow_condition,
)

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


class RuntimeSnapshotCompileError(ValueError):
    """发布快照无法转换为受控 Runtime 计划时抛出的确定性错误。"""


class CompiledRuntimePlan(BaseModel):
    """不含业务可执行代码的 Runtime 声明式计划。"""

    contract_version: str = "1.0"
    contract_hash: str
    graph_id: str
    graph_entrypoint: str
    graph_terminal_nodes: list[str]
    graph_execution_order: list[str]
    graph_node_kinds: dict[str, str]
    executor_profile: str
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


class RuntimeSnapshotArtifact(BaseModel):
    """发布事务冻结的可执行产物，Runtime 只加载而不重新解释草稿。"""

    schema_version: str = "runtime-snapshot/v1"
    snapshot_hash: str
    plan: CompiledRuntimePlan


def canonical_snapshot_hash(snapshot: dict[str, Any]) -> str:
    """计算排除自身 Artifact 后的快照哈希，避免自引用导致哈希漂移。"""
    normalized = {
        key: value for key, value in snapshot.items() if key != "runtime_artifact"
    }
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_runtime_snapshot(
    snapshot: dict[str, Any], *, tenant_id: str, agent_id: str
) -> RuntimeSnapshotArtifact:
    """在发布阶段将不可变快照编译为 Runtime 唯一接受的声明式计划。"""
    if snapshot.get("schema_version") != "1.0":
        raise RuntimeSnapshotCompileError(
            "unsupported published snapshot schema_version"
        )
    if snapshot.get("tenant_id") != tenant_id or snapshot.get("agent_id") != agent_id:
        raise RuntimeSnapshotCompileError(
            "published snapshot identity does not match compilation request"
        )
    spec = _mapping(snapshot, "spec")
    graph = _mapping(spec, "graph")
    nodes = graph.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeSnapshotCompileError("published graph has no nodes")
    node_kinds = {
        str(_required(item, "node_id")): str(_required(item, "kind"))
        for item in nodes
        if isinstance(item, dict)
    }
    if len(node_kinds) != len(nodes):
        raise RuntimeSnapshotCompileError(
            "published graph node_id values must be unique"
        )
    entrypoint = str(_required(graph, "entrypoint"))
    terminals = [str(item) for item in graph.get("terminal_nodes") or []]
    if (
        entrypoint not in node_kinds
        or not terminals
        or not set(terminals) <= node_kinds.keys()
    ):
        raise RuntimeSnapshotCompileError(
            "published graph entrypoint or terminal nodes are invalid"
        )
    edges = graph.get("edges") or []
    order = _reachable_order(entrypoint, edges, node_kinds)
    if sorted(set(node_kinds) - set(order)):
        raise RuntimeSnapshotCompileError("published graph contains unreachable nodes")
    if not set(order).intersection(terminals):
        raise RuntimeSnapshotCompileError(
            "published graph has no reachable terminal node"
        )
    workflow_policy = _workflow_policy(entrypoint, terminals, edges, node_kinds)
    prompt = _mapping(spec, "prompt")
    template = str(_required(prompt, "system_template"))
    variables = [str(item) for item in prompt.get("variables") or []]
    if set(_VARIABLE.findall(template)) - set(variables):
        raise RuntimeSnapshotCompileError(
            "published prompt contains undeclared variables"
        )
    output_schema = prompt.get("output_schema")
    if output_schema is not None:
        Draft202012Validator.check_schema(output_schema)
    model_policy = _mapping(spec, "model_policy")
    routes = {
        str(item.get("route_name")): item
        for item in model_policy.get("routes") or []
        if isinstance(item, dict) and item.get("route_name")
    }
    default_route = routes.get(str(model_policy.get("default_route")))
    if not default_route or not default_route.get("models"):
        raise RuntimeSnapshotCompileError(
            "published model policy has no executable default route"
        )
    models = [str(item) for item in default_route["models"]]
    fallback = routes.get(str(default_route.get("fallback_route")))
    fallback_models = models[1:] + (
        [str(item) for item in fallback.get("models") or []] if fallback else []
    )
    executor_profile = str(spec.get("runtime_executor", "")).strip()
    if not executor_profile:
        raise RuntimeSnapshotCompileError(
            "published runtime executor profile is missing"
        )
    if executor_profile == "code-runner/v1":
        code_runner_bindings = [
            item for item in spec.get("tools") or []
            if isinstance(item, dict) and item.get("tool_name") == "controlled_code_runner"
        ]
        if len(code_runner_bindings) != 1 or not code_runner_bindings[0].get("version"):
            raise RuntimeSnapshotCompileError(
                "code-runner/v1 requires one version-pinned controlled_code_runner tool binding"
            )
    snapshot_hash = canonical_snapshot_hash(snapshot)
    return RuntimeSnapshotArtifact(
        snapshot_hash=snapshot_hash,
        plan=CompiledRuntimePlan(
            contract_hash=snapshot_hash,
            graph_id=str(_required(graph, "graph_id")),
            graph_entrypoint=entrypoint,
            graph_terminal_nodes=terminals,
            graph_execution_order=order,
            graph_node_kinds=node_kinds,
            executor_profile=executor_profile,
            required_capabilities=required_runtime_capabilities(spec),
            workflow_policy=workflow_policy,
            prompt_template=template,
            prompt_variables=variables,
            prompt_output_schema=output_schema,
            tools=[dict(item) for item in spec.get("tools") or []],
            knowledge=[dict(item) for item in spec.get("knowledge") or []],
            logical_model=models[0],
            fallback_models=list(dict.fromkeys(fallback_models)),
            data_region=default_route.get("data_region"),
            retrieval_policy=dict(spec.get("retrieval_policy") or {}),
            subagents=[dict(item) for item in spec.get("subagents") or []],
        ),
    )


def load_runtime_snapshot_artifact(
    snapshot: dict[str, Any], *, tenant_id: str, agent_id: str
) -> CompiledRuntimePlan:
    """校验冻结 Artifact 与快照身份/哈希一致后返回计划，不进行运行时重编译。"""
    try:
        artifact = RuntimeSnapshotArtifact.model_validate(
            snapshot.get("runtime_artifact")
        )
    except Exception as exc:
        raise RuntimeSnapshotCompileError(
            "published runtime snapshot artifact is missing or invalid"
        ) from exc
    if snapshot.get("tenant_id") != tenant_id or snapshot.get("agent_id") != agent_id:
        raise RuntimeSnapshotCompileError(
            "published snapshot identity does not match the run"
        )
    expected_hash = canonical_snapshot_hash(snapshot)
    if (
        artifact.snapshot_hash != expected_hash
        or artifact.plan.contract_hash != expected_hash
    ):
        raise RuntimeSnapshotCompileError(
            "published runtime snapshot artifact hash does not match snapshot"
        )
    return artifact.plan


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
    """读取必须为对象的快照分段，拒绝隐式类型转换。"""
    item = value.get(name)
    if not isinstance(item, dict):
        raise RuntimeSnapshotCompileError(f"published snapshot {name} is missing")
    return item


def _required(value: dict[str, Any], name: str) -> Any:
    """读取必填字段并将缺失转为可定位的编译错误。"""
    item = value.get(name)
    if item is None or item == "":
        raise RuntimeSnapshotCompileError(
            f"published snapshot field is required: {name}"
        )
    return item


def _reachable_order(
    entrypoint: str, edges: list[Any], nodes: dict[str, str]
) -> list[str]:
    """计算发布图可达顺序，并拒绝引用未知端点的边。"""
    adjacency = {node: [] for node in nodes}
    for edge in edges:
        if not isinstance(edge, dict):
            raise RuntimeSnapshotCompileError("published graph edge must be an object")
        source, target = str(edge.get("from_node")), str(edge.get("to_node"))
        if source not in adjacency or target not in adjacency:
            raise RuntimeSnapshotCompileError(
                "published graph edge references an unknown node"
            )
        adjacency[source].append(target)
    queue, seen, order = deque([entrypoint]), set(), []
    while queue:
        current = queue.popleft()
        if current not in seen:
            seen.add(current)
            order.append(current)
            queue.extend(adjacency[current])
    return order


def _workflow_policy(
    entrypoint: str, terminals: list[str], edges: list[Any], nodes: dict[str, str]
) -> dict[str, Any]:
    """将已验证的 Graph DSL 编译为受限节点角色与可审计条件迁移。"""
    roles = {node: _role(node, kind, node in terminals) for node, kind in nodes.items()}
    adjacency: dict[str, list[dict[str, Any]]] = {node: [] for node in nodes}
    for edge in edges:
        source, target = str(edge.get("from_node")), str(edge.get("to_node"))
        try:
            condition = compile_workflow_condition(edge.get("condition"))
        except WorkflowConditionError as exc:
            raise RuntimeSnapshotCompileError(
                f"published graph condition is invalid: {exc}"
            ) from exc
        adjacency[source].append(
            {
                "to": target,
                "condition": condition.model_dump(mode="json") if condition else None,
            }
        )
    allowed = {
        "decision": {"retrieval", "tool", "answer", "clarify"},
        "retrieval": {"decision", "answer", "clarify"},
        "tool": {"decision", "answer", "clarify"},
        "answer": set(),
        "clarify": set(),
    }
    for node, role in roles.items():
        targets = [roles[item["to"]] for item in adjacency[node]]
        if any(target not in allowed[role] for target in targets):
            raise RuntimeSnapshotCompileError(
                f"published workflow transition is not allowed from {role} at {node}"
            )
        if role in {"retrieval", "tool"} and len(targets) != 1:
            raise RuntimeSnapshotCompileError(
                f"published {role} node must have exactly one controlled successor"
            )
        if role == "decision" and not targets:
            raise RuntimeSnapshotCompileError(
                "published decision node must declare at least one action"
            )
        if len(targets) > 1 and not all(item["condition"] for item in adjacency[node]):
            raise RuntimeSnapshotCompileError(
                f"published branching node requires conditions on every edge: {node}"
            )
    if roles[entrypoint] not in {"decision", "retrieval", "clarify"}:
        raise RuntimeSnapshotCompileError(
            "published workflow entry must be decision, retrieval or clarify"
        )
    if any(
        roles[node] not in {"answer", "clarify"} or adjacency[node]
        for node in terminals
    ):
        raise RuntimeSnapshotCompileError("published terminal node is invalid")
    return {
        "version": "workflow-policy/v1",
        "entrypoint": entrypoint,
        "terminals": terminals,
        "node_roles": roles,
        "adjacency": adjacency,
    }


def _role(node_id: str, kind: str, terminal: bool) -> str:
    """把声明节点类型归一化为 Runtime 固定的五类安全角色。"""
    value = kind.strip().lower()
    if value in {"decision", "planner"}:
        return "decision"
    if value in {"retrieval", "rag"}:
        return "retrieval"
    if value in {"tool", "action"}:
        return "tool"
    if value in {"answer", "final"}:
        return "answer"
    if value in {"clarify", "clarification"}:
        return "clarify"
    if value == "llm":
        return "answer" if terminal else "decision"
    raise RuntimeSnapshotCompileError(
        f"published graph node kind is unsupported: {node_id}:{kind}"
    )
