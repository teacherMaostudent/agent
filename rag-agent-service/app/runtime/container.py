"""Runtime composition root for graph, harness, persistence and worker adapters."""

import sqlite3

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from platform_infra.identity import build_workload_token_provider
from platform_infra.mtls import mtls_httpx_options

from app.agent.decision_engine import GatewayDecisionEngine, OfflineDecisionEngine
from app.agent.graph import AgentGraph
from app.runtime.harness import AgentHarness
from app.context.client import HttpContextClient
from app.core.config import get_settings
from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.runtime.async_jobs import AsyncRunQueue
from app.runtime.budget import BudgetGuard
from app.runtime.integration import (
    ControlPlaneClient,
    GovernanceOutboxPublisher,
    RuntimeStore,
)
from app.runtime.planner import (
    GatewaySemanticAnalyzer,
    HeuristicSemanticAnalyzer,
    RuntimePlanner,
)
from app.runtime.postgres_store import PostgresRuntimeStore
from app.runtime.temporal_queue import TemporalRunQueue
from app.tools.client import ToolGatewayClient


class AgentRuntimeContainer:
    """Assemble the execution plane without embedding business-agent policy."""
    """Composition root for Runtime; no document repository or retriever is loaded here."""

    def __init__(self, *, build_async_queue: bool = True) -> None:
        self.settings = get_settings()
        self.workload_identity = build_workload_token_provider(self.settings)
        if self.settings.persistence == "postgres":
            self.run_store = PostgresRuntimeStore(
                self.settings.database_url, self.settings.database_schema
            )
            self._checkpoint_connection = None
            self._checkpoint_context = PostgresSaver.from_conn_string(
                self.settings.database_url
            )
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
        self.context_client = HttpContextClient(
            self.settings.context_service_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
            self.workload_identity,
            self.settings.governance_delivery_mode,
        )
        self.llm_gateway = LlmGatewayClient(
            base_url=self.settings.llm_gateway_base_url,
            api_key=self.settings.llm_gateway_api_key,
            user_id="agent-runtime",
            timeout=self.settings.llm_timeout,
            workload_identity=self.workload_identity,
        )
        if self.settings.llm_startup_check:
            self.llm_gateway.healthcheck()
        decision_engine = (
            GatewayDecisionEngine(self.llm_gateway, self.settings.agent_model)
            if self.settings.llm_enabled
            else OfflineDecisionEngine()
        )
        semantic_analyzer = (
            GatewaySemanticAnalyzer(self.llm_gateway, self.settings.agent_model)
            if self.settings.llm_enabled
            else HeuristicSemanticAnalyzer()
        )
        self.tool_registry = ToolGatewayClient(
            self.settings.tool_gateway_base_url,
            self.settings.tool_gateway_api_key,
            self.settings.agent_tool_timeout,
            workload_identity=self.workload_identity,
        )
        if self.settings.tool_gateway_startup_check:
            self.tool_registry.healthcheck()
        graph = AgentGraph(
            decision_engine,
            tool_registry=self.tool_registry,
            context_client=self.context_client,
            planner=RuntimePlanner(semantic_analyzer),
            budget_guard=BudgetGuard(
                self.settings.agent_llm_call_reservation_usd,
                self.settings.agent_tool_call_reservation_usd,
            ),
            checkpointer=self.checkpointer,
            cancellation_checker=self._is_cancelled,
        )
        self.agent_harness = AgentHarness(graph)
        # Compatibility alias for integrations that still access the graph.
        # New execution paths should use agent_harness.
        self.agent_graph = self.agent_harness
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
        )
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

    def close(self) -> None:
        if self.async_runs is not None:
            self.async_runs.close()
        self.tool_registry.close()
        self.run_store.close()
        if self._checkpoint_context is not None:
            self._checkpoint_context.__exit__(None, None, None)
        if self._checkpoint_connection is not None:
            self._checkpoint_connection.close()

    def _is_cancelled(self, tenant_id: str, run_id: str) -> bool:
        run = self.run_store.get(tenant_id, run_id)
        return bool(run and run.cancel_requested)

    def _execute_submission(self, submission: dict) -> dict:
        from types import SimpleNamespace

        from app.domain.schemas import AgentRunRequest
        from app.service_api.runtime_api import run_agent

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(container=self))
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
        )
