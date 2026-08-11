package com.zxf.ai.gateway.enhancement;

import jakarta.validation.constraints.NotBlank;

/** 定义增强聊天入口的最小请求，仅接受经过校验的用户消息。 */
public record EnhancedChatRequest(
        @NotBlank
        String message
) {
}
