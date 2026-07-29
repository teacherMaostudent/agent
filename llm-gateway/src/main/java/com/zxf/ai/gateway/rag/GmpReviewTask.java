package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Map;

public record GmpReviewTask(
        String taskId,
        String ragReviewId,
        String documentId,
        String businessId,
        String documentType,
        String tenantId,
        String userId,
        String status,
        String riskLevel,
        String summary,
        boolean needHumanReview,
        BigDecimal cost,
        long latencyMs,
        JsonNode ragResponse,
        String reviewer,
        Instant reviewedAt,
        String reviewNotes,
        Map<String, Object> metadata,
        Instant createdAt,
        Instant updatedAt
) {
}
