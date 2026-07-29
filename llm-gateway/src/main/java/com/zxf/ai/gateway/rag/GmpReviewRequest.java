package com.zxf.ai.gateway.rag;

import jakarta.validation.constraints.NotBlank;

import java.util.Map;

public record GmpReviewRequest(
        String taskId,
        String documentId,
        String businessId,
        @NotBlank String documentType,
        String content,
        String model,
        String checklistVersion,
        String reviewerHint,
        Map<String, Object> metadata
) {
}
