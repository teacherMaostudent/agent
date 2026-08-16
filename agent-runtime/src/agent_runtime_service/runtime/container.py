"""Runtime composition root for graph, harness, persistence and worker adapters."""

import sqlite3

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options
from platform_sdk.clients.context import HttpContextClient
from platform_sdk.clients.llm_gateway import LlmGatewayClient
from platform_sdk.clients.rag import HttpRagQueryClient
from platform_sdk.clients.tool_gateway import ToolGatewayClient
from platform_sdk.contracts.capabilities import RuntimeCapability

from agent_runtime_service.agent.decision_engine import GatewayDecisionEngine, OfflineDecisionEngine
from agent_runtime_service.agent.graph import AgentGraph
from agent_runtime_service.core.config import get_settings
from agent_runtime_service.runtime.async_jobs import AsyncRunQueue
from agent_runtime_service.runtime.budget import BudgetGuard
from agent_runtime_service.runtime.capabilities import CapabilityRegistry, CapabilityUnavailable
from agent_runtime_service.runtime.catalog import ExecutorCatalog
from agent_runtime_service.runtime.event_bus import (
    RuntimeEventBus,
    RuntimeLifecycleEvent,
)
from agent_runtime_service.runtime.harness import (
    AgentHarness,
    DurableExecutor,
    GraphExecutor,
    SimpleExecutor,
)
from agent_runtime_service.runtime.integration import (
    ControlPlaneClient,
    GovernanceOutboxPublisher,
    RuntimeStore,
)
from agent_runtime_service.runtime.planner import (
    GatewaySemanticAnalyzer,
    HeuristicSemanticAnalyzer,
    RuntimePlanner,
)
from agent_runtime_service.runtime.postgres_store import PostgresRuntimeStore
from agent_runtime_service.runtime.subagents import SubAgentDelegation, SubAgentManager
from agent_runtime_service.runtime.temporal_queue import TemporalRunQueue


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
        # Event Bus 是进程内扩展边界，不与治理 Outbox 共用或竞争跨服务投递职责。
        self.events = RuntimeEventBus()
        self.workload_identity = build_workload_token_provider(self.settings)
        if self.settings.persistence == "postgres":
            self.run_store = PostgresRuntimeStore(
                self.settings.database_url, self.settings.database_schema
            )
            self._checkpoint_connection = None
            self._checkpoint_context = PostgresSaver.from_conn_string(self.settings.database_url)
            self.checkpointer = self._checkpoint_context.__enter__()
        else:
            self.run_store = RuntimeStore(self.settings.runtime_store_path)
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
        llm_gateway = LlmGatewayClient(
            base_url=self.settings.llm_gateway_base_url,
            api_key=self.settings.llm_gateway_api_key,
            user_id="agent-runtime",
            timeout=self.settings.llm_timeout,
            workload_identity=self.workload_identity,
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
        providers = {
            RuntimeCapability.CONTEXT: context_client,
            RuntimeCapability.RETRIEVAL: rag_client,
            RuntimeCapability.LLM: llm_gateway,
            RuntimeCapability.TOOL: tool_registry,
        }
        if self.async_runs is not None:
            providers[RuntimeCapability.WORKFLOW] = self.async_runs
        self.capabilities = CapabilityRegistry(
            providers,
            version=self.settings.capability_catalog_version,
        )
        decision_engine = (
            GatewayDecisionEngine(
                self.capability(RuntimeCapability.LLM), self.settings.agent_model
            )
            if self.settings.llm_enabled
            else OfflineDecisionEngine()
        )
        semantic_analyzer = (
            GatewaySemanticAnalyzer(
                self.capability(RuntimeCapability.LLM), self.settings.agent_model
            )
            if self.settings.llm_enabled
            else HeuristicSemanticAnalyzer()
        )
        graph = AgentGraph(
            decision_engine,
            tool_registry=self.capability(RuntimeCapability.TOOL),
            context_client=self.capability(RuntimeCapability.CONTEXT),
            rag_client=self.capability(RuntimeCapability.RETRIEVAL),
            planner=RuntimePlanner(semantic_analyzer),
            budget_guard=BudgetGuard(
                self.settings.agent_llm_call_reservation_usd,
                self.settings.agent_tool_call_reservation_usd,
            ),
            checkpointer=self.checkpointer,
            cancellation_checker=self._is_cancelled,
            subagent_manager=SubAgentManager(),
            subagent_executor=self._invoke_subagent,
        )
        # 执行器目录只在启动期装配; 请求路径不能注册或替换业务执行器。
        graph_executor = GraphExecutor(graph)
        self.executor_catalog = ExecutorCatalog(
            {
                "simple/v1": SimpleExecutor(),
                "declarative-langgraph/v1": graph_executor,
                "temporal-workflow/v1": DurableExecutor(graph_executor),
            }
        )
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
        self.capabilities.close()
        self.run_store.close()
        if self._checkpoint_context is not None:
            self._checkpoint_context.__exit__(None, None, None)
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()

    def _is_cancelled(self, tenant_id: str, run_id: str) -> bool:
        """读取持久化协作取消标记，供 Graph 在外部调用前中止。"""
        run = self.run_store.get(tenant_id, run_id)
        return bool(run and run.cancel_requested)

    def _cancel_execution(self, tenant_id: str, run_id: str):
        """先持久化协作取消，再通知对应队列/Temporal Workflow，避免长期 Workflow 残留。"""
        run, session_event = self.run_store.cancel_with_session_event(tenant_id, run_id)
        try:
            queue = self.capability(RuntimeCapability.WORKFLOW)
        except CapabilityUnavailable:
            queue = None
        if queue is not None:
            queued = queue.cancel(tenant_id, run_id)
            if run is None:
                return queued
        if session_event is not None:
            self.publish_session_event(session_event)
        return run

    def publish_session_event(self, event: RuntimeLifecycleEvent) -> None:
        """分发已事务提交的 Session 事件；事件总线绝不自行创建第二份状态事实。"""
        self.events.publish(event)

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

        from agent_runtime_service.service_api.runtime_api import resume_run, run_agent

        # Temporal 载荷仅来自已鉴权的 Runtime API 提交; 构造内部已验证身份, 避免 Worker
        # 因未经过 ASGI OIDC middleware 而退回调用方 Header 或错误拒绝。
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self)),
            scope={"auth.claims": {"worker": "agent-runtime"}},
        )
        if submission.get("operation") == "resume":
            approval = submission.get("approval") or {}
            return resume_run(
                submission["run_id"],
                AgentResumeRequest.model_validate(approval),
                request,
                x_tenant_id=submission["tenant_id"],
                x_user_id=str(approval.get("decided_by") or submission["user_id"]),
                _temporal_worker_execution=True,
            )
        return run_agent(
            AgentRunRequest.model_validate(submission["payload"]),
            request,
            x_tenant_id=submission["tenant_id"],
            x_user_id=submission["user_id"],
            x_permissions=submission["permissions"],
            x_request_id=submission["request_id"],
            x_trace_id=submission["trace_id"],
            x_run_id=submission["run_id"],
            _temporal_worker_execution=True,
            _release_resolution=submission.get("release_resolution"),
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
                "_parent_agent_id": parent_state.get("agent_id", ""),
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
                session_id=str(parent_state.get("session_id", "")) or None,
                metadata=metadata,
                max_steps=delegation.max_steps,
                max_cost_usd=delegation.max_cost_usd,
            ),
            request,
            x_tenant_id=str(parent_state["tenant_id"]),
            x_user_id=str(parent_state["user_id"]),
            x_permissions=",".join(parent_state.get("permissions", [])),
            x_trace_id=str(parent_state.get("trace_id", "")),
            _temporal_worker_execution=True,
        )
