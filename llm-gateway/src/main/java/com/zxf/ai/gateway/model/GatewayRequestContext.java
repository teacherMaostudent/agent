package com.zxf.ai.gateway.model;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

/**
 * A single model invocation's trusted execution context.
 *
 * <p>Tenant and user identities are resolved from the API key by the controller.
 * Agent metadata is supplied by the upstream Agent service and is retained for
 * quota, cost, audit and trace correlation. It is deliberately not forwarded to
 * external model providers.</p>
 */
public record GatewayRequestContext(
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
        Instant startedAt
) {
    public static GatewayRequestContext create(String requestId, String userId, String requestedModel, boolean stream) {
        return create(requestId, "public", userId, requestedModel, stream);
    }

    public static GatewayRequestContext create(String requestId, String tenantId, String userId, String requestedModel, boolean stream) {
        return create(requestId, requestId, tenantId, userId, null, null, null,
                requestId, null, null, "unspecified", requestedModel, stream);
    }

    /**
     * Creates a fully correlated context while applying stable defaults for
     * direct, non-Agent OpenAI-compatible clients.
     */
    public static GatewayRequestContext create(
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
            String requestedModel,
            boolean stream
    ) {
        return create(requestId, traceId, tenantId, userId, agentId, agentVersion,
                sessionId, runId, purpose, costBudget, "unspecified", requestedModel, stream);
    }

    public static GatewayRequestContext create(
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
            boolean stream
    ) {
        String resolvedRequestId = requestId == null || requestId.isBlank()
                ? UUID.randomUUID().toString()
                : requestId;
        String resolvedTraceId = valueOrDefault(traceId, resolvedRequestId);
        String resolvedTenantId = tenantId == null || tenantId.isBlank() ? "public" : tenantId;
        String resolvedUserId = userId == null || userId.isBlank() ? "anonymous" : userId;
        return new GatewayRequestContext(
                resolvedRequestId,
                resolvedTraceId,
                resolvedTenantId,
                resolvedUserId,
                valueOrDefault(agentId, "direct-client"),
                valueOrDefault(agentVersion, "unversioned"),
                valueOrDefault(sessionId, "stateless"),
                valueOrDefault(runId, resolvedRequestId),
                valueOrDefault(purpose, "general-model-access"),
                costBudget,
                valueOrDefault(dataRegion, "unspecified"),
                requestedModel,
                stream,
                Instant.now()
        );
    }

    private static String valueOrDefault(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value.trim();
    }
}
