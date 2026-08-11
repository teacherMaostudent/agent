package com.zxf.ai.gateway.enhancement;

/** 返回增强聊天答案、检索上下文和本次启用能力的可解释标识。 */
public record EnhancedChatResponse(
        String answer,
        String retrievedContext,
        boolean ragEnabled,
        boolean toolCallingEnabled
) {
}
