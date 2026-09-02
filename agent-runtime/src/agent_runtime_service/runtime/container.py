"""Runtime composition root for graph, harness, persistence and worker adapters."""

import sqlite3
from datetime import datetime
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options
from platform_infra.object_storage import S3ObjectStorage
from platform_infra.schema_registry import SchemaRegistry
from platform_sdk.clients.context import HttpContextClient
from platform_sdk.clients.ingestion import IngestionClient
from platform_sdk.clients.llm_gateway import LlmGatewayClient
from platform_sdk.clients.rag import HttpRagQueryClient
from platform_sdk.clients.tool_gateway import ToolGatewayClient
from platform_sdk.contracts.capabilities import RuntimeCapability
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.skills import CapabilityProviderDescriptor
from platform_sdk.contracts.workflow import CompiledWorkflowPlan
from platform_sdk.tools.registry import ToolContext

from agent_runtime_service.agent.decision_engine import GatewayDecisionEngine, OfflineDecisionEngine
from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.core.config import get_settings
from agent_runtime_service.runtime.agent_manager import AgentManager
from agent_runtime_service.runtime.async_jobs import AsyncRunQueue
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.capabilities import CapabilityRegistry, CapabilityUnavailable
from agent_runtime_service.runtime.capability_dispatcher import GovernedCapabilityDispatcher
from agent_runtime_service.runtime.capability_handlers import RuntimeCapabilityHandlers
from agent_runtime_service.runtime.catalog import ExecutorCatalog
from agent_runtime_service.runtime.code_runner import ControlledCodeRunner, SandboxPolicy
from agent_runtime_service.runtime.event_bus import (
    RuntimeEventBus,
    RuntimeInterceptionPipeline,
    RuntimeLifecycleEvent,
)
from agent_runtime_service.runtime.harness import (
    AgentHarness,
    GraphExecutor,
    SimpleExecutor,
    TemporalDurabilityAdapter,
)
from agent_runtime_service.runtime.integration import (
    ControlPlaneClient,
    GovernanceOutboxPublisher,
    RuntimeStore,
)
from agent_runtime_service.runtime.models import RuntimeBudget
from agent_runtime_service.runtime.planner import (
    GatewaySemanticAnalyzer,
    HeuristicSemanticAnalyzer,
    RuntimePlanner,
)
from agent_runtime_service.runtime.postgres_store import PostgresRuntimeStore
from agent_runtime_service.runtime.run_state import AgentRunEvent, InvalidRunTransition
from agent_runtime_service.runtime.runtime_context import RuntimeContext
from agent_runtime_service.runtime.session_archive import SessionArchiveService
from agent_runtime_service.runtime.session_events import ModelVisibleMessage, RuntimeEventType
from agent_runtime_service.runtime.skill_runtime import GatewaySkillExecutor
from agent_runtime_service.runtime.stop_policy import (
    BudgetStopPolicy,
    CancellationStopPolicy,
    CompositeStopPolicy,
)
from agent_runtime_service.runtime.subagents import SubAgentDelegation, SubAgentManager
from agent_runtime_service.runtime.temporal_queue import TemporalRunQueue
from agent_runtime_service.runtime.workflow_runtime import ZeroAgentWorkflowRuntime


class AgentRuntimeContainer:
    """Compose the execution plane without loading RAG repositories or retrievers.

    Runtime may own durable execution state, but it obtains memory and evidence
    from their service APIs so a worker cannot bypass their ACL boundaries.
    """

    def __init__(self, *, build_async_queue: bool = True) -> None:
        """按配置组合执行平面的依赖。

        Runtime 只经 SDK HTTP 契约访问 Context/RAG/Tool Gateway；任一启动健康检查失败都
        阻止接收请求，避免半可用 Agent。
        """
        self.settings = get_settings()
        self.schema_registry = SchemaRegistry(self.settings.contracts_schema_dir)
        self.session_archive = (
            SessionArchiveService(
                S3ObjectStorage(
                    bucket=self.settings.session_archive_bucket,
                    prefix=self.settings.session_archive_prefix,
                    endpoint_url=self.settings.session_archive_endpoint_url,
                    region=self.settings.session_archive_region,
                    kms_key_id=self.settings.session_archive_kms_key_id,
                ),
                retention_days=self.settings.session_archive_retention_days,
                compliance_mode=self.settings.session_archive_compliance_mode,
            )
            if self.settings.session_archive_enabled
            else None
        )
        # Event Bus 是进程内扩展边界，不与治理 Outbox 共用或竞争跨服务投递职责。
        self.events = RuntimeEventBus()
        # 当前没有请求期插件；冻结注册窗口保证会话事实不会被临时回调悄然改变语义。
        self.events.freeze()
        self.interception_pipeline = RuntimeInterceptionPipeline()
        self.workload_identity = build_workload_token_provider(self.settings)
        if self.settings.persistence == "postgres":
            self.run_store = PostgresRuntimeStore(
                self.settings.database_url,
                self.settings.database_schema,
                self.schema_registry,
            )
            self._checkpoint_connection = None
            self._checkpoint_context = PostgresSaver.from_conn_string(self.settings.database_url)
            self.checkpointer = self._checkpoint_context.__enter__()
        else:
            self.run_store = RuntimeStore(self.settings.runtime_store_path, self.schema_registry)
            self._checkpoint_context = None
            self._checkpoint_connection = sqlite3.connect(
                self.settings.runtime_checkpoint_path,
                check_same_thread=False,
            )
            self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.checkpointer.setup()
        context_client = HttpContextClient(
            self.settings.context_service_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
            self.workload_identity,
            mtls=self._mtls_options(),
        )
        rag_client = HttpRagQueryClient(
            self.settings.rag_query_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
            self.workload_identity,
            mtls=self._mtls_options(),
        )
        self.ingestion = IngestionClient(
            self.settings.ingestion_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
            self.workload_identity,
            mtls=self._mtls_options(),
        )
        llm_gateway = LlmGatewayClient(
            base_url=self.settings.llm_gateway_base_url,
            api_key=self.settings.llm_gateway_api_key,
            user_id="agent-runtime",
            timeout=self.settings.llm_timeout,
            workload_identity=self.workload_identity,
            mtls=self._mtls_options(),
        )
        if self.settings.llm_startup_check:
            llm_gateway.healthcheck()
        tool_registry = ToolGatewayClient(
            self.settings.tool_gateway_base_url,
            self.settings.tool_gateway_api_key,
            self.settings.agent_tool_timeout,
            workload_identity=self.workload_identity,
        )
        if self.settings.tool_gateway_startup_check:
            tool_registry.healthcheck()
        # 队列在启动期创建且随后冻结进能力目录。异步回调仅在请求完成装配后触发,
        # 因而不会形成 API/Worker 两套状态机。
        self.async_runs = None
        if build_async_queue:
            self.async_runs = (
                TemporalRunQueue(
                    self.settings.temporal_target,
                    self.settings.temporal_namespace,
                    self.settings.temporal_runtime_task_queue,
                    self.settings.temporal_region_targets,
                )
                if self.settings.temporal_enabled
                else AsyncRunQueue(self.settings.runtime_jobs_path, self._execute_submission)
            )
        agent_manager = AgentManager(SubAgentManager(), self._invoke_subagent)
        providers = {
            RuntimeCapability.CONTEXT: context_client,
            RuntimeCapability.RETRIEVAL: rag_client,
            RuntimeCapability.LLM: llm_gateway,
            RuntimeCapability.TOOL: tool_registry,
            RuntimeCapability.SESSION: self.run_store,
            RuntimeCapability.SUBAGENT: agent_manager,
        }
        if self.async_runs is not None:
            providers[RuntimeCapability.WORKFLOW] = self.async_runs
        if self.settings.code_runner_enabled:
            sandbox_policy = SandboxPolicy()
            providers[RuntimeCapability.SANDBOX] = sandbox_policy
            providers[RuntimeCapability.CODE_RUNNER] = ControlledCodeRunner(
                tool_registry, sandbox_policy
            )
        self.capabilities = CapabilityRegistry(
            providers,
            version=self.settings.capability_catalog_version,
        )
        # Graph/Executor 只获得这一强类型能力视图；CapabilityRegistry 仍保留给
        # 发布证明和 Profile 校验，不能被业务节点当作任意对象目录读取。
        self.runtime_context = RuntimeContext(
            context=context_client,
            retrieval=rag_client,
            llm=llm_gateway,
            tools=tool_registry,
            session=self.run_store,
            workflow=self.async_runs,
            agents=agent_manager,
        )
        self.skill_executor = GatewaySkillExecutor(self.runtime_context)
        decision_engine = (
            GatewayDecisionEngine(self.runtime_context.require_llm(), self.settings.agent_model)
            if self.settings.llm_enabled
            else OfflineDecisionEngine()
        )
        semantic_analyzer = (
            GatewaySemanticAnalyzer(self.runtime_context.require_llm(), self.settings.agent_model)
            if self.settings.llm_enabled
            else HeuristicSemanticAnalyzer()
        )
        budget_guard = BudgetGuard(
            self.settings.agent_llm_call_reservation_usd,
            self.settings.agent_tool_call_reservation_usd,
        )
        graph = AgentGraph(
            decision_engine,
            self.runtime_context,
            planner=RuntimePlanner(semantic_analyzer),
            budget_guard=budget_guard,
            checkpointer=self.checkpointer,
            cancellation_checker=self._is_cancelled,
            agent_manager=agent_manager,
            session_event_recorder=self._record_graph_session_event,
            interception_pipeline=self.interception_pipeline,
            stop_policy=CompositeStopPolicy(
                (
                    BudgetStopPolicy(budget_guard),
                    CancellationStopPolicy(self._is_cancelled),
                )
            ),
            mailbox=self.run_store,
            capability_handler_factory=self._capability_handlers,
        )
        # 执行器目录只在启动期装配; 请求路径不能注册或替换业务执行器。
        graph_executor = GraphExecutor(graph)
        executor_profiles = {
            "simple/v1": SimpleExecutor(),
            "agentic/v1": graph_executor,
            "declarative-langgraph/v1": graph_executor,
            "temporal-simple/v1": TemporalDurabilityAdapter(SimpleExecutor()),
            "temporal-agentic/v1": TemporalDurabilityAdapter(graph_executor),
            "temporal-workflow/v1": TemporalDurabilityAdapter(graph_executor),
        }
        if self.settings.code_runner_enabled:
            # Code Runner still uses the bounded Graph; its Snapshot grants only the remote sandbox tool.
            executor_profiles["code-runner/v1"] = graph_executor
        self.executor_catalog = ExecutorCatalog(executor_profiles)
        self.control_plane = (
            ControlPlaneClient(
                self.settings.control_plane_base_url,
                self.settings.control_plane_runtime_key,
                self.settings.service_http_timeout,
                self.workload_identity,
                mtls=mtls_httpx_options(
                    enabled=self.settings.mtls_enabled,
                    ca_file=self.settings.mtls_ca_file,
                    cert_file=self.settings.mtls_cert_file,
                    key_file=self.settings.mtls_key_file,
                ),
            )
            if self.settings.control_plane_base_url
            else None
        )
        self.governance = GovernanceOutboxPublisher(
            self.run_store,
            self.settings.governance_base_url,
            self.settings.governance_event_key,
            self.settings.service_http_timeout,
            self.workload_identity,
            self.settings.governance_delivery_mode,
            mtls=self._mtls_options(),
        )
        self.agent_harness = AgentHarness(
            release_resolver=self.control_plane,
            executor_resolver=self.executor_catalog,
            fallback_model=self.settings.agent_model,
            snapshot_required=self.settings.snapshot_required,
            cancel_execution=self._cancel_execution,
            capability_resolver=self.capabilities,
        )

    def close(self) -> None:
        """按依赖反向顺序关闭队列、客户端、存储和检查点连接。"""
        self.ingestion.close()
        self.capabilities.close()
        self.run_store.close()
        if self._checkpoint_context is not None:
            self._checkpoint_context.__exit__(None, None, None)
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()

    def _capability_handlers(self, state: dict[str, Any]) -> dict:
        """为 Agent 当前步骤创建缩小权限/预算的 Provider 处理器。"""
        budget = RuntimeBudget.model_validate(state["budget"])
        permissions = frozenset(str(item) for item in state.get("permissions", []))
        return RuntimeCapabilityHandlers(
            self,
            permissions=permissions,
            budget=budget,
            plan_id=str(state.get("execution_plan", {}).get("plan_id", "")),
            plan_admission_id=str(state.get("plan_admission", {}).get("admission_id", "")),
            step_id=f"step-{int(state.get('step_count', 0)) + 1}",
            workflow_runner=lambda provider, payload, context: self._run_embedded_workflow(
                provider,
                payload,
                context,
                permissions=permissions,
                budget=budget,
                depth=0,
            ),
        ).handlers()

    def _run_embedded_workflow(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
        *,
        permissions: frozenset[str],
        budget: RuntimeBudget,
        depth: int,
    ) -> dict[str, Any]:
        """在 Agent 所有的 RootTask 内执行有界 Workflow，Owner 仍为 AGENT。"""
        if depth >= 4:
            raise RuntimeError("embedded workflow composition exceeds depth limit")
        if self.control_plane is None:
            raise RuntimeError("Control Plane is required for Workflow capability")
        resolution = self.control_plane.resolve_workflow(
            context.tenant_id,
            provider.provider_id,
            str(payload.get("environment", "production")),
            context.trace_id,
        )
        plan = CompiledWorkflowPlan.model_validate(resolution.get("plan"))
        digest = str(resolution.get("artifact_digest", ""))
        if plan.version != provider.version or (
            provider.artifact_digest and provider.artifact_digest != digest
        ):
            raise RuntimeError("Workflow provider version or artifact digest drift")
        if str(payload.get("launch_mode", "same_root")) == "independent":
            return self._launch_independent_workflow(
                plan,
                resolution,
                payload,
                context,
                permissions=permissions,
                budget=budget,
            )
        nested_handlers = RuntimeCapabilityHandlers(
            self,
            permissions=permissions,
            budget=budget,
            workflow_runner=lambda child, child_payload, child_context: self._run_embedded_workflow(
                child,
                child_payload,
                child_context,
                permissions=permissions,
                budget=budget,
                depth=depth + 1,
            ),
        )
        dispatcher = GovernedCapabilityDispatcher(
            plan.capability_providers,
            plan.capability_routing,
            nested_handlers.handlers(),
        )
        result = ZeroAgentWorkflowRuntime(dispatcher.dispatch_output).run(
            plan,
            dict(payload.get("input") or payload),
            context.model_copy(update={"workflow_id": plan.workflow_id}),
            embedded=True,
        )
        return result.model_dump(mode="json")

    def _launch_independent_workflow(
        self,
        plan: CompiledWorkflowPlan,
        resolution: dict[str, Any],
        payload: dict[str, Any],
        context: ExecutionContext,
        *,
        permissions: frozenset[str],
        budget: RuntimeBudget,
    ) -> dict[str, Any]:
        """启动拥有新 RootTask 的 Workflow，原 Agent 只获得运行引用。"""
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import WorkflowRunRequest

        from agent_runtime_service.service_api.runtime_api import run_workflow

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        return run_workflow(
            WorkflowRunRequest(
                workflow_id=plan.workflow_id,
                environment=str(payload.get("environment", "production")),
                input=dict(payload.get("input") or payload),
                deadline_seconds=max(1, int(context.remaining_seconds())),
                max_cost_usd=budget.remaining_cost_usd,
            ),
            request,
            x_tenant_id=context.tenant_id,
            x_user_id=context.user_id,
            x_permissions=",".join(sorted(permissions)),
            x_request_id=f"{context.request_id}-workflow",
            x_trace_id=context.trace_id,
            _release_resolution=resolution,
        )

    def _run_agent_capability(
        self,
        provider: CapabilityProviderDescriptor,
        payload: dict[str, Any],
        context: ExecutionContext,
        *,
        permissions: frozenset[str],
        budget: RuntimeBudget,
    ) -> dict[str, Any]:
        """Workflow 调用独立 Agent 运行，子 Agent 重新解析自己的 Release。"""
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import AgentRunRequest

        from agent_runtime_service.service_api.runtime_api import run_agent

        if self.control_plane is None:
            raise RuntimeError("Control Plane is required for Agent capability")
        session_id = f"workflow-agent-{uuid4().hex}"
        resolution = self.control_plane.resolve(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            agent_id=provider.provider_id,
            environment=str(payload.get("environment", "production")),
            session_id=session_id,
            trace_id=context.trace_id,
        )
        snapshot = dict(resolution.get("snapshot") or {})
        semantic = str(snapshot.get("agent_version", "")).split(":")[-1]
        digest = str((snapshot.get("runtime_artifact") or {}).get("snapshot_hash", ""))
        if semantic != provider.version or (
            provider.artifact_digest and digest != provider.artifact_digest
        ):
            raise RuntimeError("Agent provider version or artifact digest drift")
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        task = payload.get("task")
        if task is None and isinstance(payload.get("input"), dict):
            task = payload["input"].get("task")
        task_text = str(task or "").strip()
        if not task_text:
            raise ValueError("Agent capability requires a non-empty task")
        return run_agent(
            AgentRunRequest(
                task=task_text,
                agent_id=provider.provider_id,
                environment=str(payload.get("environment", "production")),
                session_id=session_id,
                metadata={
                    "_parent_run_id": context.run_id,
                    "_root_task_id": context.root_task_id,
                },
                max_steps=min(30, budget.max_steps),
                max_cost_usd=budget.remaining_cost_usd,
            ),
            request,
            x_tenant_id=context.tenant_id,
            x_user_id=context.user_id,
            x_permissions=",".join(sorted(permissions)),
            x_request_id=f"{context.request_id}-agent",
            x_trace_id=context.trace_id,
            _release_resolution=resolution,
            _orchestration_owner=context.orchestration_owner,
            _workflow_id=context.workflow_id,
        )

    def _is_cancelled(self, tenant_id: str, run_id: str) -> bool:
        """读取持久化协作取消标记，供 Graph 在外部调用前中止。"""
        run = self.run_store.get(tenant_id, run_id)
        return bool(run and run.cancel_requested)

    def _cancel_execution(self, tenant_id: str, run_id: str):
        """按子到根传播取消，再通知每个队列/Temporal Workflow，避免孤儿子任务继续运行。"""
        run_ids = [*reversed(self.run_store.descendant_run_ids(tenant_id, run_id)), run_id]
        cancelled: dict[str, object] = {}
        session_events = []
        for candidate in run_ids:
            run, session_event = self.run_store.cancel_with_session_event(tenant_id, candidate)
            if run is not None:
                cancelled[candidate] = run
            if session_event is not None:
                session_events.append(session_event)
        try:
            queue = self.capability(RuntimeCapability.WORKFLOW)
        except CapabilityUnavailable:
            queue = None
        if queue is not None:
            for candidate in run_ids:
                queue.cancel(tenant_id, candidate)
        for event in session_events:
            self.publish_session_event(event)
        return cancelled.get(run_id)

    def publish_session_event(self, event: RuntimeLifecycleEvent) -> None:
        """分发已事务提交的 Session 事件；事件总线绝不自行创建第二份状态事实。"""
        self.events.publish(event)

    def reconcile_tool_intents(self, context: ExecutionContext) -> None:
        """在重放未完成 Run 前向 Tool Gateway 对账，阻止不确定副作用被盲目重试。

        `COMPLETED` 表示 Graph 可以使用相同幂等键安全读取回放结果；`IN_PROGRESS`
        必须交给 Temporal 延后重试，`NOT_FOUND` 才允许首次实际执行。该检查不把
        Gateway 响应写成 ToolResult，避免绕过 Graph 的观察值与 Prompt 边界。
        """
        intents = self.run_store.unresolved_tool_intents(
            context.tenant_id, context.session_id, context.run_id
        )
        tool_client = self.capability(RuntimeCapability.TOOL)
        for intent in intents:
            tool_name = str(intent.metadata.get("tool_name", ""))
            execution_id = str(intent.metadata.get("tool_execution_id", ""))
            if not tool_name or not execution_id or not hasattr(tool_client, "execution_status"):
                raise RuntimeError("unrecoverable tool intent lacks a Gateway execution identity")
            state = tool_client.execution_status(
                tool_name,
                ToolContext(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    request_id=context.request_id,
                    trace_id=context.trace_id,
                    run_id=context.run_id,
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    agent_version=context.agent_version,
                    snapshot_id=context.snapshot_id,
                    deadline_at=context.deadline_at.isoformat(),
                    attempt_budget_remaining=context.attempt_budget_remaining,
                    tool_execution_id=execution_id,
                    idempotency_key=execution_id,
                    tool_version=str(intent.metadata.get("tool_version", "")),
                ),
            )
            if state.get("status") == "IN_PROGRESS":
                raise RuntimeError(
                    f"tool execution {execution_id} is still in progress; recovery must wait"
                )

    def archive_session(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        """把完整会话账本归档到对象存储，并在本地账本保存只读定位与校验摘要。"""
        if self.session_archive is None:
            raise RuntimeError("session archive storage is not configured")
        payload = self.run_store.session_archive_payload(tenant_id, session_id)
        events = payload.get("events", [])
        if not events:
            raise RuntimeError("cannot archive an empty session ledger")
        key, checksum = self.session_archive.archive(tenant_id, session_id, payload)
        self.run_store.record_session_archive(
            tenant_id,
            session_id,
            archive_key=key,
            archive_sha256=checksum,
            archived_through_sequence=int(events[-1]["sequence"]),
        )
        return self.run_store.latest_session_archive(tenant_id, session_id) or {}

    def _record_graph_session_event(
        self,
        state: dict[str, Any],
        event_type: RuntimeEventType,
        metadata: dict[str, Any],
        model_message: ModelVisibleMessage | None,
    ) -> None:
        """把 Graph 的已完成步骤追加为受限 Session 事实，并在提交后通知本地观察者。

        Graph 只持有标识字段而不持有 Store；这里集中恢复 ``ExecutionContext``，使任何
        Prompt、模型、工具或子 Agent 事件都沿用与 Run 相同的租户、快照和追踪边界。
        """
        context = ExecutionContext(
            request_id=str(state["request_id"]),
            trace_id=str(state["trace_id"]),
            session_id=str(state["session_id"]),
            tenant_id=str(state["tenant_id"]),
            user_id=str(state["user_id"]),
            agent_id=str(state["agent_id"]),
            agent_version=str(state["agent_version"]),
            snapshot_id=str(state["snapshot_id"]),
            release_id=str(state.get("release_id", "")),
            release_stage=str(state.get("release_stage", "production")),
            release_projection_revision=int(state.get("release_projection_revision", 1)),
            traffic_policy_version=str(state.get("traffic_policy_version", "traffic-policy/v1")),
            side_effect_policy_version=str(state.get("side_effect_policy_version", "side-effect-policy/v1")),
            graph_version=str(state["graph_version"]),
            model_policy_version=str(
                state.get("agent_snapshot", {}).get("model_policy_version", "unknown")
            ),
            run_id=str(state["run_id"]),
            deadline_at=datetime.fromisoformat(str(state["deadline_at"])),
            attempt_budget_remaining=int(state.get("attempt_budget_remaining", 0)),
            parent_run_id=str(state.get("metadata", {}).get("_parent_run_id", "")),
            parent_session_id=str(state.get("metadata", {}).get("_parent_session_id", "")),
            orchestration_owner=str(state.get("orchestration_owner", "agent")),
            workflow_id=str(state.get("workflow_id", "")),
        )
        event = self.run_store.append_session_event(
            context,
            event_type,
            metadata=metadata,
            model_message=model_message,
            turn_id=str(state.get("turn_id", "")),
            step_id=str(metadata.get("step_id", "")),
            epoch_id=str(metadata.get("epoch_id", "")),
            attempt_id=str(state.get("attempt_id", "")),
        )
        self.publish_session_event(event)
        # 图事件只提供运行事实；显式 Run 状态机在这里将其归约为有限执行阶段。
        # 未映射的事件（如 Prompt/证据）不会伪造状态变化，仍完整保留在 Session Ledger。
        trigger = {
            RuntimeEventType.CONTEXT_INJECTED: AgentRunEvent.CONTEXT_READY,
            RuntimeEventType.MODEL_REQUESTED: AgentRunEvent.MODEL_REQUESTED,
            RuntimeEventType.TOOL_INTENT_RECORDED: AgentRunEvent.TOOL_INTENT_RECORDED,
            RuntimeEventType.TOOL_RESULT: AgentRunEvent.TOOLS_COMPLETED,
            RuntimeEventType.STEERING_RECEIVED: AgentRunEvent.STEERING_RECEIVED,
            RuntimeEventType.FOLLOW_UP_RECEIVED: AgentRunEvent.FOLLOW_UP_RECEIVED,
        }.get(event_type)
        if trigger is None:
            return
        try:
            _, state_event = self.run_store.transition_state(
                context.tenant_id,
                context.run_id,
                trigger,
                metadata={
                    "source_event_id": event.event_id,
                    "source_event_type": event.event_type.value,
                },
            )
        except InvalidRunTransition as exc:
            # 不能吞掉状态漂移；否则副作用完成但 Run 生命周期会变得不可解释。
            raise RuntimeError(
                f"run state transition rejected after {event.event_type.value}"
            ) from exc
        if state_event is not None:
            self.publish_session_event(state_event)

    def capability(self, capability: RuntimeCapability):
        """从冻结目录取得外围 Provider，禁止业务代码直接依赖某个服务客户端属性。"""
        return self.capabilities.require(capability)

    def _mtls_options(self) -> dict:
        """为全部内部 HTTP 客户端连接生成 Runtime 专属 mTLS 身份配置。"""
        return mtls_httpx_options(
            enabled=self.settings.mtls_enabled,
            ca_file=self.settings.mtls_ca_file,
            cert_file=self.settings.mtls_cert_file,
            key_file=self.settings.mtls_key_file,
        )

    def _execute_submission(self, submission: dict) -> dict:
        """让异步 Worker 复用唯一同步运行入口，防止 API 与 Worker 状态机分叉。"""
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import AgentResumeRequest, AgentRunRequest

        from agent_runtime_service.runtime.mailbox import ClaimedRunMailboxItem, RunMailboxInputType
        from agent_runtime_service.runtime.models import UserInputResume
        from agent_runtime_service.service_api.runtime_api import resume_run, run_agent

        # Temporal 载荷仅来自已鉴权的 Runtime API 提交; 构造内部已验证身份, 避免 Worker
        # 因未经过 ASGI OIDC middleware 而退回调用方 Header 或错误拒绝。
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        if submission.get("shadow_mirror"):
            return self._execute_shadow_submission(submission)
        if submission.get("operation") == "resume":
            control_input = submission.get("control_input") or {}
            user_input = control_input.get("_user_input")
            claimed_control = control_input.get("_claimed_control")
            return resume_run(
                submission["run_id"],
                AgentResumeRequest.model_validate(control_input),
                request,
                x_tenant_id=submission["tenant_id"],
                x_user_id=str(control_input.get("decided_by") or submission["user_id"]),
                _temporal_worker_execution=True,
                _user_input=(
                    UserInputResume.model_validate(user_input)
                    if isinstance(user_input, dict)
                    else None
                ),
                _claimed_control=(
                    ClaimedRunMailboxItem(
                        message_id=str(claimed_control["message_id"]),
                        input_type=RunMailboxInputType(str(claimed_control["input_type"])),
                        lease_token=str(claimed_control["lease_token"]),
                        control_input=dict(claimed_control.get("control_input") or {}),
                    )
                    if isinstance(claimed_control, dict)
                    else None
                ),
            )
        return run_agent(
            AgentRunRequest.model_validate(submission["payload"]),
            request,
            x_tenant_id=submission["tenant_id"],
            x_user_id=submission["user_id"],
            x_permissions=submission["permissions"],
            x_roles=",".join(str(item) for item in submission.get("subject_roles", [])),
            x_request_id=submission["request_id"],
            x_trace_id=submission["trace_id"],
            x_run_id=submission["run_id"],
            _temporal_worker_execution=True,
            _release_resolution=submission.get("release_resolution"),
        )

    def submit_shadow_mirror(self, submission: dict[str, Any]) -> dict[str, Any] | None:
        """持久化一条独立 Shadow 回放，不影响主任务成功、延迟或用户结果。

        入口只接受 Runtime 已冻结的提交信息，并使用同一队列获得重试/幂等行为。没有
        启用开关、没有队列或没有 Control Plane 时显式返回 ``None``，绝不悄悄同步执行。
        """
        if (
            not self.settings.shadow_mirroring_enabled
            or self.async_runs is None
            or self.control_plane is None
        ):
            return None
        return self.async_runs.submit({**submission, "shadow_mirror": True})

    def _execute_shadow_submission(self, submission: dict[str, Any]) -> dict:
        """执行候选 Snapshot 的隔离镜像；未采样或未配置候选均是正常跳过。

        Shadow 不读取/写入用户原 Session，而是为 ``source_run_id`` 建独立会话；工具网关
        根据 Release Stage 强制模拟写操作。其答案不回传 Workspace，只有治理 Trace 用于
        GateDecision。这里不把模拟结果解释为真实业务成功。
        """
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import AgentRunRequest

        from agent_runtime_service.runtime.integration import ReleaseNotFoundError
        from agent_runtime_service.service_api.runtime_api import run_agent

        if self.control_plane is None:
            return {"status": "SKIPPED", "shadow_status": "control_plane_unavailable"}
        payload = AgentRunRequest.model_validate(submission["payload"])
        source_session_id = str(payload.session_id or submission["request_id"])
        try:
            resolution = self.control_plane.resolve_shadow(
                tenant_id=str(submission["tenant_id"]),
                user_id="shadow-worker",
                agent_id=payload.agent_id,
                environment=payload.environment,
                session_id=source_session_id,
                trace_id=f"shadow:{submission['trace_id']}",
                subject_roles=frozenset(str(item) for item in submission.get("subject_roles", [])),
            )
        except ReleaseNotFoundError:
            return {"status": "SKIPPED", "shadow_status": "no_active_shadow_release"}
        if not bool(resolution.get("shadow_sampled")):
            return {"status": "SKIPPED", "shadow_status": "not_sampled"}
        release_id = str(resolution.get("release_id") or "unknown")
        shadow_run_id = str(submission["run_id"])
        isolated_payload = payload.model_copy(
            update={
                "session_id": f"shadow:{release_id}:{submission['source_run_id']}",
                "metadata": {
                    **payload.metadata,
                    "_shadow_internal": True,
                    "_shadow_source_run_id": submission["source_run_id"],
                    "_shadow_source_session_id": source_session_id,
                },
            }
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime", "shadow": True}},
        )
        result = run_agent(
            isolated_payload,
            request,
            x_tenant_id=str(submission["tenant_id"]),
            x_user_id="shadow-worker",
            x_permissions=str(submission["permissions"]),
            x_roles=",".join(str(item) for item in submission.get("subject_roles", [])),
            x_request_id=str(submission["request_id"]),
            x_trace_id=f"shadow:{submission['trace_id']}",
            x_run_id=shadow_run_id,
            _temporal_worker_execution=True,
            _release_resolution=resolution,
        )
        return {
            **result,
            "shadow_mirror_of": submission["source_run_id"],
            "shadow_status": "completed",
        }

    def _execute_workflow_submission(self, submission: dict) -> dict:
        """让 Temporal Activity 复用唯一 Workflow API 执行逻辑和契约验证。"""
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import WorkflowRunRequest

        from agent_runtime_service.service_api.runtime_api import run_workflow

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        return run_workflow(
            WorkflowRunRequest.model_validate(submission["payload"]),
            request,
            x_tenant_id=submission["tenant_id"],
            x_user_id=submission["user_id"],
            x_permissions=submission["permissions"],
            x_request_id=submission["request_id"],
            x_trace_id=submission["trace_id"],
            _temporal_worker_execution=True,
            _release_resolution=submission.get("release_resolution"),
            _run_id=submission["run_id"],
            _checkpoint=submission.get("checkpoint"),
            _signal=submission.get("signal"),
        )

    def _invoke_subagent(
        self, delegation: SubAgentDelegation, task: str, parent_state: dict
    ) -> dict:
        """以内部受信身份启动子运行；子 Agent 必须重新解析自己的 Release 和 Snapshot。"""
        from types import SimpleNamespace

        from platform_sdk.contracts.runtime_api import AgentRunRequest

        from agent_runtime_service.service_api.runtime_api import run_agent

        metadata = dict(parent_state.get("metadata", {}))
        metadata.update(
            {
                "_subagent_depth": delegation.depth,
                "_parent_run_id": parent_state.get("run_id", ""),
                "_parent_session_id": parent_state.get("session_id", ""),
                "_parent_agent_id": parent_state.get("agent_id", ""),
                "_root_task_id": delegation.root_task_id,
                "_collaboration_snapshot_id": delegation.collaboration_snapshot_id,
                # Fallback/重试共享同一业务操作键，Tool Gateway 可先对账再决定是否执行。
                "_business_operation_id": delegation.business_operation_id,
            }
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        return run_agent(
            AgentRunRequest(
                task=task,
                agent_id=delegation.target_agent_id,
                environment=str(metadata.get("runtime_environment", "production")),
                # 子 Agent 追加到自己的会话账本；父会话只保留委派/结果事实，不能混写。
                session_id=f"session_{uuid4().hex}",
                metadata=metadata,
                max_steps=delegation.max_steps,
                max_cost_usd=delegation.max_cost_usd,
            ),
            request,
            x_tenant_id=str(parent_state["tenant_id"]),
            x_user_id=str(parent_state["user_id"]),
            # 子 Agent 只收到父权限与发布 delegated scope 的交集，委派链绝不扩权。
            x_permissions=",".join(sorted(delegation.delegated_permissions)),
            x_trace_id=str(parent_state.get("trace_id", "")),
            _temporal_worker_execution=True,
        )
