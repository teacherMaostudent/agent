package com.zxf.ai.gateway.enhancement;

import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.service.AiServices;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * LangChain4j 增强层 Bean 配置。
 */
@Configuration
@EnableConfigurationProperties(LangChain4jProperties.class)
@ConditionalOnProperty(prefix = "enhancement.langchain4j", name = "enabled", havingValue = "true")
public class LangChain4jConfig {
    /** 依据受控配置创建 OpenAI 兼容聊天模型，不在此处处理路由或租户鉴权。 */
    @Bean
    public ChatModel langChain4jChatModel(LangChain4jProperties properties) {
        return OpenAiChatModel.builder()
                .baseUrl(properties.getBaseUrl())
                .apiKey(properties.getApiKey())
                .modelName(properties.getModelName())
                .timeout(properties.getTimeout())
                .build();
    }

    /** 将模型、检索器和受注册工具组装为 LangChain4j AI Service。 */
    @Bean
    public GatewayAssistant gatewayAssistant(
            ChatModel langChain4jChatModel,
            GatewayTools gatewayTools,
            SimpleKnowledgeBase simpleKnowledgeBase
    ) {
        return AiServices.builder(GatewayAssistant.class)
                .chatModel(langChain4jChatModel)
                .contentRetriever(simpleKnowledgeBase)
                .tools(gatewayTools)
                .build();
    }
}
