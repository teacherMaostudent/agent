import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.decision_engine import GatewayDecisionEngine, OfflineDecisionEngine
from app.agent.graph import AgentGraph
from app.context.client import HttpContextClient
from app.core.config import get_settings
from app.infrastructure.llm_gateway_client import LlmGatewayClient
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
from app.tools.client import ToolGatewayClient


class AgentRuntimeContainer:
    """Composition root for Runtime; no document repository or retriever is loaded here."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.run_store = RuntimeStore(self.settings.runtime_store_path)
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
        )
        self.llm_gateway = LlmGatewayClient(
            base_url=self.settings.llm_gateway_base_url,
            api_key=self.settings.llm_gateway_api_key,
            user_id="agent-runtime",
            timeout=self.settings.llm_timeout,
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
        )
        if self.settings.tool_gateway_startup_check:
            self.tool_registry.healthcheck()
        self.agent_graph = AgentGraph(
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
        self.control_plane = (
            ControlPlaneClient(
                self.settings.control_plane_base_url,
                self.settings.control_plane_runtime_key,
                self.settings.service_http_timeout,
            )
            if self.settings.control_plane_base_url
            else None
        )
        self.governance = GovernanceOutboxPublisher(
            self.run_store,
            self.settings.governance_base_url,
            self.settings.governance_event_key,
            self.settings.service_http_timeout,
        )

    def close(self) -> None:
        self.tool_registry.close()
        self.run_store.close()
        self._checkpoint_connection.close()

    def _is_cancelled(self, tenant_id: str, run_id: str) -> bool:
        run = self.run_store.get(tenant_id, run_id)
        return bool(run and run.cancel_requested)
