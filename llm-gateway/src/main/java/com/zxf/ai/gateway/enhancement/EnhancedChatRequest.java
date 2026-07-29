package com.zxf.ai.gateway.enhancement;

import jakarta.validation.constraints.NotBlank;

public record EnhancedChatRequest(
        @NotBlank
        String message
) {
}
