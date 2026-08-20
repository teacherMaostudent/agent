package com.zxf.ai.gateway.web;

import com.zxf.ai.gateway.enhancement.EnhancedChatRequest;
import com.zxf.ai.gateway.enhancement.EnhancedChatResponse;
import com.zxf.ai.gateway.enhancement.EnhancedChatService;
import jakarta.validation.Valid;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

/**
 * LangChain4j 增强层接口。
 *
 * <p>这个接口和 /v1/chat/completions 分开，表示它不是基础模型网关转发能力，
 * 而是建立在网关项目上的 RAG + Tool Calling 应用能力。</p>
 */
@RestController
@RequestMapping("/v1/enhanced")
@ConditionalOnProperty(prefix = "enhancement.langchain4j", name = "enabled", havingValue = "true")
public class EnhancedChatController {
    private final EnhancedChatService enhancedChatService;

    /**
     * 初始化 enhanced chat controller 所需的依赖与运行期状态。
    */
    public EnhancedChatController(EnhancedChatService enhancedChatService) {
        this.enhancedChatService = enhancedChatService;
    }

    @PostMapping(path = "/chat", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    /**
     * 执行增强聊天入口，并把上下文、RAG 与模型调用委托给既有服务边界。
    */
    public Mono<EnhancedChatResponse> chat(@Valid @RequestBody EnhancedChatRequest request) {
        return enhancedChatService.chat(request);
    }
}
