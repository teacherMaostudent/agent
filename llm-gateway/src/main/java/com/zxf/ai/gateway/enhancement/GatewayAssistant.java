package com.zxf.ai.gateway.enhancement;

import dev.langchain4j.service.SystemMessage;
import dev.langchain4j.service.UserMessage;

/**
 * LangChain4j AI Service 接口。
 *
 * <p>AI Service 会把普通 Java 接口动态代理成 LLM 调用入口。这里的 {@link SystemMessage}
 * 用来约束回答风格：优先使用 RAG 上下文，必要时调用工具，最后给出适合后端工程面试的解释。</p>
 */
public interface GatewayAssistant {
    @SystemMessage("""
            你是一个 Java 后端与大模型应用架构助手。
            回答时优先使用系统补充的知识库上下文。
            如果问题涉及时间、成本、限额、模型路由或网关状态，可以调用可用工具。
            回答要具体，尽量结合 LLM Gateway、Spring Boot、WebFlux、RAG、Tool Calling 场景。
            """)
    /**
     * 将用户问题交给受系统提示词和工具约束的 LangChain4j 服务执行。
     */
    String chat(@UserMessage String message);
}
