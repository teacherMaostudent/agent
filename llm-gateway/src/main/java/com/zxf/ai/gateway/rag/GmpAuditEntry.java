package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;

import java.math.BigDecimal;
import java.time.Instant;

public record GmpAuditEntry(
        String id,
        String taskId,
        Instant timestamp,
        String actor,
        String action,
        String status,
        String riskLevel,
        BigDecimal cost,
        long latencyMs,
        JsonNode payload,
        String notes
) {
}
