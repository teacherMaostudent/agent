package com.zxf.ai.gateway.enhancement;

import org.springframework.stereotype.Service;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * LangChain4j 增强聊天服务。
 *
 * <p>LangChain4j 的 AI Service 当前以同步调用为主。为了不阻塞 WebFlux 事件循环，
 * 这里把调用包装到 boundedElastic 线程池中执行。这样 Controller 仍然可以保持响应式接口。</p>
 */
@Service
@ConditionalOnProperty(prefix = "enhancement.langchain4j", name = "enabled", havingValue = "true")
public class EnhancedChatService {
    private final GatewayAssistant gatewayAssistant;
    private final SimpleKnowledgeBase knowledgeBase;

    /** 注入 AI Service 与知识检索器，保持增强层依赖可替换。 */
    public EnhancedChatService(GatewayAssistant gatewayAssistant, SimpleKnowledgeBase knowledgeBase) {
        this.gatewayAssistant = gatewayAssistant;
        this.knowledgeBase = knowledgeBase;
    }

    /** 在线程隔离池中执行同步 AI Service 调用，避免阻塞 WebFlux 事件循环。 */
    public Mono<EnhancedChatResponse> chat(EnhancedChatRequest request) {
        return Mono.fromCallable(() -> {
                    String context = knowledgeBase.retrieveContext(request.message(), 3);
                    // RAG 上下文由 LangChain4j 的 ContentRetriever 自动注入。
                    // 这里单独取一次 context 只是为了把检索结果返回给前端，方便调试和演示。
                    String answer = gatewayAssistant.chat(request.message());
                    return new EnhancedChatResponse(answer, context, true, true);
                })
                .subscribeOn(Schedulers.boundedElastic());
    }
}
