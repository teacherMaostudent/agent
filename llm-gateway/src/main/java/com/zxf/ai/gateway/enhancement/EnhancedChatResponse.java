package com.zxf.ai.gateway.enhancement;

public record EnhancedChatResponse(
        String answer,
        String retrievedContext,
        boolean ragEnabled,
        boolean toolCallingEnabled
) {
}
