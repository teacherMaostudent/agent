package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;

public record GmpHumanReviewRequest(
        String reviewer,
        String action,
        String finalRiskLevel,
        String finalSummary,
        JsonNode finalResult,
        String notes
) {
}
