from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

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
    prompt_template: str
    prompt_variables: list[str] = Field(default_factory=list)
    prompt_output_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    logical_model: str
    fallback_models: list[str] = Field(default_factory=list)
    data_region: str | None = None
    retrieval_policy: dict[str, Any] = Field(default_factory=dict)

    @property
    def retrieval_top_k(self) -> int:
        return max((int(item.get("top_k", 8)) for item in self.knowledge), default=8)

    @property
    def knowledge_filters(self) -> dict[str, Any]:
        return {
            str(item["knowledge_base"]): item.get("filters", {})
            for item in self.knowledge
        }


def compile_snapshot(
    snapshot: dict[str, Any],
    *,
    tenant_id: str,
    agent_id: str,
    fallback_model: str,
) -> CompiledAgentPlan:
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
    order = _reachable_order(entrypoint, graph.get("edges") or [], node_kinds)
    unreachable = sorted(set(node_kinds) - set(order))
    if unreachable:
        raise SnapshotCompileError(f"published graph contains unreachable nodes: {unreachable}")
    if not set(order).intersection(terminals):
        raise SnapshotCompileError("published graph has no reachable terminal node")

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

    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CompiledAgentPlan(
        contract_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        graph_id=str(_required(graph, "graph_id")),
        graph_entrypoint=entrypoint,
        graph_terminal_nodes=terminals,
        graph_execution_order=order,
        graph_node_kinds=node_kinds,
        prompt_template=template,
        prompt_variables=declared,
        prompt_output_schema=output_schema,
        tools=[dict(item) for item in spec.get("tools") or []],
        knowledge=[dict(item) for item in spec.get("knowledge") or []],
        logical_model=models[0],
        fallback_models=list(dict.fromkeys(fallback_models)),
        data_region=default_route.get("data_region"),
        retrieval_policy=dict(spec.get("retrieval_policy") or {}),
    )


def render_prompt(plan: dict[str, Any], variables: dict[str, Any]) -> str:
    template = str(plan.get("prompt_template", ""))
    used = set(_VARIABLE.findall(template))
    missing = sorted(name for name in used if _resolve(variables, name) is None)
    if missing:
        raise SnapshotCompileError(f"published prompt variables are missing: {missing}")

    def replace(match: re.Match[str]) -> str:
        value = _resolve(variables, match.group(1))
        return str(value) if value is not None else ""

    return _VARIABLE.sub(replace, template)


def validate_tool_manifests(plan: dict[str, Any], manifests: list[dict[str, Any]]) -> None:
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


def _reachable_order(
    entrypoint: str, edges: list[Any], node_kinds: dict[str, str]
) -> list[str]:
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


def _mapping(value: dict[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise SnapshotCompileError(f"published snapshot {name} is missing")
    return item


def _required(value: dict[str, Any], name: str) -> Any:
    item = value.get(name)
    if item is None or item == "":
        raise SnapshotCompileError(f"published snapshot field is required: {name}")
    return item


def _resolve(values: dict[str, Any], path: str) -> Any:
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _local_plan(model: str) -> CompiledAgentPlan:
    return CompiledAgentPlan(
        contract_hash="local-unversioned",
        graph_id="runtime-default",
        graph_entrypoint="agent-loop",
        graph_terminal_nodes=["agent-loop"],
        graph_execution_order=["agent-loop"],
        graph_node_kinds={"agent-loop": "agent"},
        prompt_template="",
        logical_model=model,
    )
