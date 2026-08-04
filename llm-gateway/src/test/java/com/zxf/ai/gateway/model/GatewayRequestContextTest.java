package com.zxf.ai.gateway.model;

import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.assertj.core.api.Assertions.assertThat;

class GatewayRequestContextTest {
    @Test
    void retainsCompleteAgentIdentityForAuditAndCostGovernance() {
        GatewayRequestContext context = GatewayRequestContext.create(
                "req-1", "trace-1", "tenant-1", "user-1",
                "general-agent", "2026.07.25", "session-1", "run-1",
                "enterprise-assistant", new BigDecimal("2.50"), "deepseek-v4-flash", true
        );

        assertThat(context.requestId()).isEqualTo("req-1");
        assertThat(context.traceId()).isEqualTo("trace-1");
        assertThat(context.tenantId()).isEqualTo("tenant-1");
        assertThat(context.userId()).isEqualTo("user-1");
        assertThat(context.agentId()).isEqualTo("general-agent");
        assertThat(context.agentVersion()).isEqualTo("2026.07.25");
        assertThat(context.sessionId()).isEqualTo("session-1");
        assertThat(context.runId()).isEqualTo("run-1");
        assertThat(context.purpose()).isEqualTo("enterprise-assistant");
        assertThat(context.costBudget()).isEqualByComparingTo("2.50");
        assertThat(context.stream()).isTrue();
    }

    @Test
    void suppliesExplicitDefaultsForDirectOpenAiCompatibleClients() {
        GatewayRequestContext context =
                GatewayRequestContext.create(null, "tenant-1", "user-1", "model-1", false);

        assertThat(context.requestId()).isNotBlank();
        assertThat(context.traceId()).isEqualTo(context.requestId());
        assertThat(context.agentId()).isEqualTo("direct-client");
        assertThat(context.agentVersion()).isEqualTo("unversioned");
        assertThat(context.sessionId()).isEqualTo("stateless");
        assertThat(context.runId()).isEqualTo(context.requestId());
        assertThat(context.purpose()).isEqualTo("general-model-access");
        assertThat(context.costBudget()).isNull();
    }
}
