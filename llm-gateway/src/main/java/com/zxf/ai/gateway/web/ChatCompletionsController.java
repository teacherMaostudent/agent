package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.service.ChatGatewayService;
import com.zxf.ai.gateway.usage.QuotaService;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.math.BigDecimal;
import java.util.Map;

@RestController
@RequestMapping("/v1")
public class ChatCompletionsController {
    private final ChatGatewayService chatGatewayService;
    private final QuotaService quotaService;
    private final ApiKeyService apiKeyService;

    public ChatCompletionsController(ChatGatewayService chatGatewayService, QuotaService quotaService, ApiKeyService apiKeyService) {
        this.chatGatewayService = chatGatewayService;
        this.quotaService = quotaService;
        this.apiKeyService = apiKeyService;
    }

    /**
     * OpenAI 兼容的非流式聊天接口。
     *
     * <p>Controller 层只做协议入口适配：读取请求头、完成 API Key 鉴权、构造网关上下文，
     * 然后把模型路由、限额、fallback、缓存、成本和性能统计交给 {@link ChatGatewayService}。</p>
     */
    @PostMapping(path = "/chat/completions", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    public Mono<JsonNode> chatCompletions(
            @RequestHeader HttpHeaders headers,
            @RequestBody JsonNode request
    ) {
        boolean stream = request.path("stream").asBoolean(false);
        GatewayRequestContext context = requestContext(headers, request, stream);
        // JSON 接口只处理非流式请求。流式请求需要让客户端使用 Accept: text/event-stream，
        // 这样 Spring WebFlux 才会匹配下面的 streamChatCompletions 方法。
        if (stream) {
            return Mono.error(new IllegalArgumentException("Use Accept: text/event-stream for stream=true"));
        }
        return chatGatewayService.complete(context, request);
    }

    /**
     * OpenAI 兼容的 SSE 流式聊天接口。
     *
     * <p>大模型生成长文本时，用户通常希望边生成边看到结果。
     * 这里使用 Flux 表示连续数据流，WebFlux 会把上游模型返回的 chunk 持续写回客户端。</p>
     */
    @PostMapping(path = "/chat/completions", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<String> streamChatCompletions(
            @RequestHeader HttpHeaders headers,
            @RequestBody JsonNode request
    ) {
        GatewayRequestContext context = requestContext(headers, request, true);
        return chatGatewayService.stream(context, request);
    }

    /**
     * Resolves the caller identity once for both JSON and SSE paths.
     *
     * <p>The API key is authoritative for tenant and user. A caller may repeat
     * {@code X-Tenant-Id} for end-to-end correlation, but it cannot use that
     * header to cross a tenant boundary.</p>
     */
    private GatewayRequestContext requestContext(HttpHeaders headers, JsonNode request, boolean stream) {
        String claimedUserId = headers.getFirst("X-User-Id");
        String model = request.path("model").asText(null);
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(
                headers.getFirst(HttpHeaders.AUTHORIZATION),
                headers.getFirst("X-Api-Key"),
                headers.getFirst("X-Tenant-Id"),
                claimedUserId,
                model
        );
        String claimedTenantId = headers.getFirst("X-Tenant-Id");
        if (claimedTenantId != null && !claimedTenantId.isBlank()
                && !claimedTenantId.equals(auth.tenantId())) {
            throw new GatewayException(HttpStatus.FORBIDDEN,
                    "X-Tenant-Id does not match the tenant bound to the API key");
        }

        String requestId = headers.getFirst("X-Request-Id");
        return GatewayRequestContext.create(
                requestId,
                headers.getFirst("X-Trace-Id"),
                auth.tenantId(),
                auth.userId(),
                headers.getFirst("X-Agent-Id"),
                headers.getFirst("X-Agent-Version"),
                headers.getFirst("X-Session-Id"),
                headers.getFirst("X-Run-Id"),
                headers.getFirst("X-Purpose"),
                parseCostBudget(headers.getFirst("X-Cost-Budget")),
                headers.getFirst("X-Data-Region"),
                model,
                stream
        );
    }

    /** Parses a non-negative request-scoped budget in the gateway base currency. */
    private BigDecimal parseCostBudget(String rawBudget) {
        if (rawBudget == null || rawBudget.isBlank()) {
            return null;
        }
        try {
            BigDecimal budget = new BigDecimal(rawBudget.trim());
            if (budget.signum() < 0) {
                throw new NumberFormatException("negative");
            }
            return budget;
        } catch (NumberFormatException error) {
            throw new GatewayException(HttpStatus.BAD_REQUEST,
                    "X-Cost-Budget must be a non-negative decimal");
        }
    }

    /**
     * 查看当前用户当天的网关用量快照。
     *
     * <p>这是一个轻量调试接口，便于观察 token 和成本统计是否生效。
     * 生产环境通常会扩展为按用户、租户、应用维度查询，并持久化到 Redis/MySQL/ClickHouse。</p>
     */
    @GetMapping("/usage/me")
    public Map<String, Object> usage(@RequestHeader(value = "X-User-Id", required = false) String userId) {
        return quotaService.snapshot(userId == null || userId.isBlank() ? "anonymous" : userId);
    }
}
