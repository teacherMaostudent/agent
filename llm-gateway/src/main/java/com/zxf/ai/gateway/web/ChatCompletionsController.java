package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.admission.AdmissionControl;
import com.zxf.ai.gateway.admission.AdmissionLease;
import com.zxf.ai.gateway.admission.AdmissionMetrics;
import com.zxf.ai.gateway.admission.AdmissionRejectedException;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.service.ChatGatewayService;
import com.zxf.ai.gateway.usage.QuotaService;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;

@RestController
@RequestMapping("/v1")
public class ChatCompletionsController {
    private final ChatGatewayService chatGatewayService;
    private final QuotaService quotaService;
    private final ApiKeyService apiKeyService;
    private final AdmissionControl admissionControl;
    private final AdmissionMetrics admissionMetrics;
    private final ApplicationEventPublisher eventPublisher;
    private final com.fasterxml.jackson.databind.ObjectMapper objectMapper;

    /**
     * 初始化 chat completions controller 所需的依赖与运行期状态。
    */
    public ChatCompletionsController(ChatGatewayService chatGatewayService, QuotaService quotaService,
                                     ApiKeyService apiKeyService, AdmissionControl admissionControl,
                                     AdmissionMetrics admissionMetrics, ApplicationEventPublisher eventPublisher,
                                     com.fasterxml.jackson.databind.ObjectMapper objectMapper) {
        this.chatGatewayService = chatGatewayService;
        this.quotaService = quotaService;
        this.apiKeyService = apiKeyService;
        this.admissionControl = admissionControl;
        this.admissionMetrics = admissionMetrics;
        this.eventPublisher = eventPublisher;
        this.objectMapper = objectMapper;
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
        admissionMetrics.executionStarted();
        admissionControl.validateRequest(request);
        AdmissionLease lease = admitIngress(context, request);
        // JSON 接口只处理非流式请求。流式请求需要让客户端使用 Accept: text/event-stream，
        // 这样 Spring WebFlux 才会匹配下面的 streamChatCompletions 方法。
        if (stream) {
            lease.release();
            return Mono.error(new IllegalArgumentException("Use Accept: text/event-stream for stream=true"));
        }
        // 入口并发覆盖缓存命中、模型调用和响应写回，取消/错误同样通过 doFinally 归还许可。
        return chatGatewayService.complete(context, request).doFinally(ignored -> lease.release());
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
        admissionMetrics.executionStarted();
        admissionControl.validateRequest(request);
        AdmissionLease lease = admitIngress(context, request);
        return chatGatewayService.stream(context, request).doFinally(ignored -> lease.release());
    }

    /**
     * 从可信认证结果、请求头和请求体构造路由上下文，统一 deadline、budget 与 trace 字段。
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

    /**
     * 入口被本地策略拦截时，仍然发布一条脱敏 Trace 和治理事件。
     *
     * <p>此前这类请求在进入服务层前就返回 429，导致审计中缺失“为什么没有执行”的
     * 证据；这里保持 HTTP 同步失败语义，但把事实异步交给既有治理发布器。</p>
     */
    private AdmissionLease admitIngress(GatewayRequestContext context, JsonNode request) {
        try {
            return admissionControl.admitIngress(context);
        } catch (AdmissionRejectedException rejected) {
            eventPublisher.publishEvent(new GatewayTraceEvent(
                    context.requestId(), context.traceId(), context.tenantId(), context.userId(),
                    context.agentId(), context.agentVersion(), context.sessionId(), context.runId(),
                    context.purpose(), context.costBudget(), context.dataRegion(), context.requestedModel(), context.stream(),
                    context.startedAt(), Instant.now(), Duration.between(context.startedAt(), Instant.now()).toMillis(),
                    request == null ? objectMapper.createObjectNode() : request.deepCopy(), objectMapper.createObjectNode(), false,
                    rejected.getClass().getSimpleName(), rejected.getMessage(), BigDecimal.ZERO, "",
                    rejected.reasonCode(), rejected.scope(), rejected.retryAfterSeconds()));
            throw rejected;
        }
    }

    /** Parses a non-negative request-scoped budget in the gateway base currency. */
    /**
     * 解析并校验调用方成本预算；负数、非法精度或无效文本在路由前拒绝。
    */
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
    public Map<String, Object> usage(@RequestHeader HttpHeaders headers) {
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(
                headers.getFirst(HttpHeaders.AUTHORIZATION),
                headers.getFirst("X-Api-Key"),
                headers.getFirst("X-Tenant-Id"),
                headers.getFirst("X-User-Id"),
                null
        );
        return quotaService.snapshot(auth.tenantId(), auth.userId());
    }
}
