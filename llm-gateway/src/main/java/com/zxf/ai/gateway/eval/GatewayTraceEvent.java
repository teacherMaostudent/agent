package com.zxf.ai.gateway.eval;

import com.fasterxml.jackson.databind.JsonNode;

import java.math.BigDecimal;
import java.time.Instant;

/** 一次网关请求的最终 Trace，只在最终成功或所有 fallback 均失败后发布一次。 */
public record GatewayTraceEvent(
        String requestId,
        String traceId,
        String tenantId,
        String userId,
        String agentId,
        String agentVersion,
        String sessionId,
        String runId,
        String purpose,
        BigDecimal costBudget,
        String dataRegion,
        String requestedModel,
        boolean stream,
        Instant startedAt,
        Instant completedAt,
        long latencyMs,
        JsonNode request,
        JsonNode response,
        boolean success,
        String errorType,
        String errorMessage,
        BigDecimal cost,
        String currency
) {
    /**
     * Compatibility constructor for internal callers that do not originate
     * from an Agent run. New model-access paths should use the full identity.
     */
    public GatewayTraceEvent(
            String requestId,
            String tenantId,
            String userId,
            String requestedModel,
            boolean stream,
            Instant startedAt,
            Instant completedAt,
            long latencyMs,
            JsonNode request,
            JsonNode response,
            boolean success,
            String errorType,
            String errorMessage,
            BigDecimal cost,
            String currency
    ) {
        this(requestId, requestId, tenantId, userId, "direct-client", "unversioned",
                "stateless", requestId, "general-model-access", null, "unspecified", requestedModel,
                stream, startedAt, completedAt, latencyMs, request, response, success,
                errorType, errorMessage, cost, currency);
    }
}
