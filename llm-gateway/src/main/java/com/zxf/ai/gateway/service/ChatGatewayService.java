package com.zxf.ai.gateway.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.cache.RequestCacheService;
import com.zxf.ai.gateway.client.LlmClientRegistry;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayUsage;
import com.zxf.ai.gateway.model.ModelEndpoint;
import com.zxf.ai.gateway.prompt.PromptTemplateService;
import com.zxf.ai.gateway.report.UsageReportService;
import com.zxf.ai.gateway.report.ModelPerformanceService;
import com.zxf.ai.gateway.resilience.GatewayPolicyService;
import com.zxf.ai.gateway.routing.ModelRouter;
import com.zxf.ai.gateway.usage.CostCalculator;
import com.zxf.ai.gateway.usage.QuotaService;
import com.zxf.ai.gateway.usage.TokenEstimator;
import com.zxf.ai.gateway.usage.OutputTokenPrediction;
import com.zxf.ai.gateway.usage.OutputTokenPredictor;
import com.zxf.ai.gateway.usage.UsageReservation;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.time.Instant;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

@Service
public class ChatGatewayService {
    private static final Logger log = LoggerFactory.getLogger(ChatGatewayService.class);

    private final ModelRouter modelRouter;
    private final LlmClientRegistry clientRegistry;
    private final TokenEstimator tokenEstimator;
    private final CostCalculator costCalculator;
    private final QuotaService quotaService;
    private final PromptTemplateService promptTemplateService;
    private final RequestCacheService requestCacheService;
    private final GatewayPolicyService policyService;
    private final UsageReportService usageReportService;
    private final ModelPerformanceService performanceService;
    private final OutputTokenPredictor outputTokenPredictor;
    private final ObjectMapper objectMapper;
    private final ApplicationEventPublisher eventPublisher;

    public ChatGatewayService(
            ModelRouter modelRouter,
            LlmClientRegistry clientRegistry,
            TokenEstimator tokenEstimator,
            CostCalculator costCalculator,
            QuotaService quotaService,
            PromptTemplateService promptTemplateService,
            RequestCacheService requestCacheService,
            GatewayPolicyService policyService,
            UsageReportService usageReportService,
            ModelPerformanceService performanceService,
            OutputTokenPredictor outputTokenPredictor,
            ObjectMapper objectMapper,
            ApplicationEventPublisher eventPublisher
    ) {
        this.modelRouter = modelRouter;
        this.clientRegistry = clientRegistry;
        this.tokenEstimator = tokenEstimator;
        this.costCalculator = costCalculator;
        this.quotaService = quotaService;
        this.promptTemplateService = promptTemplateService;
        this.requestCacheService = requestCacheService;
        this.policyService = policyService;
        this.usageReportService = usageReportService;
        this.performanceService = performanceService;
        this.outputTokenPredictor = outputTokenPredictor;
        this.objectMapper = objectMapper;
        this.eventPublisher = eventPublisher;
    }

    public Mono<JsonNode> complete(GatewayRequestContext context, JsonNode request) {
        // 非流式请求可以安全缓存：同一租户、同一请求体命中时直接返回深拷贝结果，
        // 避免重复消耗上游 token。流式请求在 RequestCacheService 中会被跳过。
        /*
         * 先把 prompt_template/template_id + variables 渲染成标准 messages。
         *
         * 这样后续缓存、token 预估、路由、日志和上游调用使用的都是“最终 Prompt”，
         * 避免出现“缓存按模板变量命中，但真实请求按展开后 Prompt 消耗”的口径不一致。
         */
        JsonNode preparedRequest = promptTemplateService.apply(request);
        return requestCacheService.cachedOrCompute(context.tenantId(), preparedRequest,
                        () -> completeUncached(context, preparedRequest))
                .doOnSuccess(response -> publishTrace(context, preparedRequest, response, true, null))
                .doOnError(error -> publishTrace(context, preparedRequest, null, false, error));
    }

    private Mono<JsonNode> completeUncached(GatewayRequestContext context, JsonNode request) {
        // 先按 prompt 预估 token 并预留额度，避免请求已经打到上游后才发现用户超额。
        // completion token 和真实成本会在模型成功返回后再补记。
        /*
         * token 预估必须基于渲染后的 request。
         * 如果请求使用了 Prompt 模板，真正发给模型的是展开后的 system/user/messages，
         * 不是 prompt_template 和 variables 这两个控制字段本身。
         */
        /*
         * ModelRouter 根据 request.model 选择调用计划：
         * - 先判断是否命中 canary 灰度模型；
         * - 未命中灰度时按 weighted 权重分流；
         * - 没有权重配置时走 primary；
         * - 最后追加 fallbacks 作为失败兜底链路。
         */
        List<ModelEndpoint> endpoints = modelRouter.resolve(request.path("model").asText(context.requestedModel()));
        ReservationPlan plan = reserveUsage(context, endpoints, request);
        return tryComplete(context, request, endpoints, 0, plan, null);
    }

    public Flux<String> stream(GatewayRequestContext context, JsonNode request) {
        /*
         * 流式请求同样先渲染 Prompt 模板。
         * 区别是流式响应不能像普通 JSON 响应一样整体缓存，所以 RequestCacheService 不参与 stream 链路。
         */
        JsonNode preparedRequest = promptTemplateService.apply(request);
        List<ModelEndpoint> endpoints = modelRouter.resolve(preparedRequest.path("model").asText(context.requestedModel()));
        ReservationPlan plan = reserveUsage(context, endpoints, preparedRequest);
        return tryStream(context, preparedRequest, endpoints, 0, plan);
    }

    private Mono<JsonNode> tryComplete(
            GatewayRequestContext context,
            JsonNode request,
            List<ModelEndpoint> endpoints,
            int index,
            ReservationPlan plan,
            Throwable lastError
    ) {
        if (index >= endpoints.size()) {
            quotaService.release(context.userId(), plan.reservation());
            String lastMessage = lastError == null ? "unknown" : lastError.getMessage();
            return Mono.error(new GatewayException(HttpStatus.BAD_GATEWAY,
                    "All model routes failed. Last error: " + lastMessage));
        }

        ModelEndpoint endpoint = endpoints.get(index);
        Instant started = Instant.now();
        if (!policyService.isAvailable(endpoint)) {
            // 熔断中的路由不再触发真实调用，直接尝试下一个 fallback，保护故障上游。
            return tryComplete(context, request, endpoints, index + 1, plan,
                    new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "Circuit is open for route: " + endpoint.key()));
        }
        try {
            policyService.beforeCall(endpoint);
        } catch (GatewayException ex) {
            return tryComplete(context, request, endpoints, index + 1, plan, ex);
        }

        return clientRegistry.resolve(endpoint).chatCompletion(endpoint, request)
                .map(response -> {
                    // 厂商返回 usage 时优先采用真实 completion token；缺失时再使用估算值。
                    long completionTokens = completionTokens(response, endpoint);
                    long actualPromptTokens = promptTokens(response, tokenEstimator.estimatePromptTokens(endpoint, request));
                    boolean providerReported = hasProviderUsage(response);
                    GatewayUsage usage = GatewayUsage.of(actualPromptTokens, completionTokens,
                            costCalculator.estimate(endpoint, response, actualPromptTokens, completionTokens),
                            costCalculator.baseCurrency(), providerReported ? "PROVIDER_RESPONSE" : "LOCAL_TOKENIZER",
                            providerReported ? "REPORTED" : "ESTIMATED");
                    quotaService.settle(context.userId(), plan.reservation(), usage);
                    outputTokenPredictor.observe(endpoint, request, completionTokens);
                    policyService.recordSuccess(endpoint);
                    logSuccess(context, endpoint, usage, plan.prediction(), started);
                    return enrich(response, endpoint, usage, plan.prediction());
                })
                .onErrorResume(error -> {
                    // 单个路由失败不直接返回给业务方，而是进入 fallback 链路。
                    // 最后一个路由也失败时，tryComplete 会把最后一次错误原因带到 502 响应中。
                    policyService.recordFailure(endpoint);
                    performanceService.recordFailure(context, endpoint, error, Duration.between(started, Instant.now()).toMillis());
                    log.warn("llm_gateway_fallback requestId={} user={} failedRoute={} reason={}",
                            context.requestId(), context.userId(), endpoint.key(), error.getMessage());
                    return tryComplete(context, request, endpoints, index + 1, plan, error);
                });
    }

    private Flux<String> tryStream(
            GatewayRequestContext context,
            JsonNode request,
            List<ModelEndpoint> endpoints,
            int index,
            ReservationPlan plan
    ) {
        if (index >= endpoints.size()) {
            quotaService.release(context.userId(), plan.reservation());
            return Flux.error(new GatewayException(HttpStatus.BAD_GATEWAY, "All model routes failed"));
        }

        ModelEndpoint endpoint = endpoints.get(index);
        Instant started = Instant.now();
        if (!policyService.isAvailable(endpoint)) {
            return tryStream(context, request, endpoints, index + 1, plan);
        }
        try {
            policyService.beforeCall(endpoint);
        } catch (GatewayException ex) {
            return tryStream(context, request, endpoints, index + 1, plan);
        }

        AtomicReference<StringBuilder> text = new AtomicReference<>(new StringBuilder());
        AtomicLong completionTokens = new AtomicLong();
        AtomicReference<Long> ttftMs = new AtomicReference<>();
        AtomicReference<JsonNode> reportedUsage = new AtomicReference<>();
        AtomicBoolean responseStarted = new AtomicBoolean();
        return clientRegistry.resolve(endpoint).streamChatCompletion(endpoint, request)
                .doOnNext(chunk -> {
                    responseStarted.set(true);
                    StreamChunk parsed = parseStreamChunk(chunk);
                    if (parsed.usage() != null) {
                        reportedUsage.set(parsed.usage());
                    }
                    // TTFT 以第一个有效内容 delta 为准，不能把心跳或 usage-only chunk 当成首 token。
                    if (!parsed.text().isBlank() && ttftMs.get() == null) {
                        ttftMs.set(Duration.between(started, Instant.now()).toMillis());
                    }
                    text.get().append(parsed.text());
                    completionTokens.set(tokenEstimator.estimateCompletionTokens(endpoint, text.get().toString()));
                })
                .doOnComplete(() -> {
                    JsonNode providerUsage = reportedUsage.get();
                    boolean reported = providerUsage != null;
                    long actualPromptTokens = reported
                            ? promptTokensFromUsage(providerUsage, tokenEstimator.estimatePromptTokens(endpoint, request))
                            : tokenEstimator.estimatePromptTokens(endpoint, request);
                    long actualCompletionTokens = reported
                            ? completionTokensFromUsage(providerUsage, completionTokens.get()) : completionTokens.get();
                    ObjectNode syntheticResponse = objectMapper.createObjectNode();
                    if (providerUsage != null) syntheticResponse.set("usage", providerUsage);
                    GatewayUsage usage = GatewayUsage.of(actualPromptTokens, actualCompletionTokens,
                            costCalculator.estimate(endpoint, syntheticResponse, actualPromptTokens, actualCompletionTokens),
                            costCalculator.baseCurrency(), reported ? "PROVIDER_STREAM" : "LOCAL_TOKENIZER",
                            reported ? "REPORTED" : "ESTIMATED");
                    quotaService.settle(context.userId(), plan.reservation(), usage);
                    outputTokenPredictor.observe(endpoint, request, actualCompletionTokens);
                    policyService.recordSuccess(endpoint);
                    logSuccess(context, endpoint, usage, plan.prediction(), started, ttftMs.get());
                    ObjectNode traceResponse = objectMapper.createObjectNode();
                    traceResponse.put("model", endpoint.upstreamModel());
                    traceResponse.putObject("choices").put("content", text.get().toString());
                    ObjectNode gateway = traceResponse.putObject("gateway");
                    gateway.put("provider", endpoint.providerName());
                    gateway.put("model", endpoint.modelName());
                    gateway.put("costEstimated", usage.cost());
                    gateway.put("costCurrency", usage.currency());
                    publishTrace(context, request, traceResponse, true, null);
                })
                .onErrorResume(error -> {
                    policyService.recordFailure(endpoint);
                    performanceService.recordFailure(context, endpoint, error, Duration.between(started, Instant.now()).toMillis());
                    log.warn("llm_gateway_stream_fallback requestId={} user={} failedRoute={} reason={}",
                            context.requestId(), context.userId(), endpoint.key(), error.getMessage());
                    if (responseStarted.get()) {
                        quotaService.release(context.userId(), plan.reservation());
                        return Flux.error(new GatewayException(
                                HttpStatus.BAD_GATEWAY,
                                "Upstream stream failed after response started; fallback was suppressed"
                        ));
                    }
                    return tryStream(context, request, endpoints, index + 1, plan);
                })
                .doOnError(error -> {
                    if (index == 0) publishTrace(context, request, null, false, error);
                });
    }

    private JsonNode enrich(JsonNode response, ModelEndpoint endpoint, GatewayUsage usage,
                            OutputTokenPrediction prediction) {
        if (response instanceof ObjectNode objectNode) {
            // 在兼容 OpenAI 响应的基础上追加 gateway 元信息，便于调试路由、成本和上游模型。
            ObjectNode gateway = objectNode.putObject("gateway");
            gateway.put("provider", endpoint.providerName());
            gateway.put("model", endpoint.modelName());
            gateway.put("upstreamModel", endpoint.upstreamModel());
            gateway.put("promptTokensEstimated", usage.promptTokens());
            gateway.put("completionTokensEstimated", usage.completionTokens());
            gateway.put("costEstimated", usage.cost());
            gateway.put("costCurrency", usage.currency());
            gateway.put("usageSource", usage.usageSource());
            gateway.put("costStatus", usage.costStatus());
            gateway.put("priceVersion", endpoint.model().getPriceVersion());
            ObjectNode forecast = gateway.putObject("outputTokenPrediction");
            forecast.put("p50", prediction.p50());
            forecast.put("p90", prediction.p90());
            forecast.put("p95", prediction.p95());
            forecast.put("p99", prediction.p99());
            forecast.put("reserved", prediction.selected());
            forecast.put("conformalCorrection", prediction.conformalCorrection());
            forecast.put("sampleCount", prediction.sampleCount());
            forecast.put("modelVersion", prediction.modelVersion());
        }
        return response;
    }

    private long completionTokens(JsonNode response, ModelEndpoint endpoint) {
        JsonNode usage = response.path("usage");
        if (usage.has("completion_tokens")) {
            return usage.path("completion_tokens").asLong();
        }
        StringBuilder builder = new StringBuilder();
        JsonNode choices = response.path("choices");
        if (choices.isArray()) {
            for (JsonNode choice : choices) {
                builder.append(choice.path("message").path("content").asText(""));
            }
        }
        return tokenEstimator.estimateCompletionTokens(endpoint, builder.toString());
    }

    private long promptTokens(JsonNode response, long fallback) {
        return promptTokensFromUsage(response.path("usage"), fallback);
    }

    private long promptTokensFromUsage(JsonNode usage, long fallback) {
        if (usage.has("prompt_tokens")) {
            return usage.path("prompt_tokens").asLong();
        }
        if (usage.has("input_tokens")) {
            // Anthropic 将普通输入、cache read 和 cache creation 分列；总 Prompt 用量必须求和。
            return usage.path("input_tokens").asLong()
                    + usage.path("cache_read_input_tokens").asLong(0)
                    + usage.path("cache_creation_input_tokens").asLong(0);
        }
        return fallback;
    }

    private long completionTokensFromUsage(JsonNode usage, long fallback) {
        if (usage.has("completion_tokens")) return usage.path("completion_tokens").asLong();
        if (usage.has("output_tokens")) return usage.path("output_tokens").asLong();
        return fallback;
    }

    private boolean hasProviderUsage(JsonNode response) {
        JsonNode usage = response.path("usage");
        return usage.has("prompt_tokens") || usage.has("input_tokens")
                || usage.has("completion_tokens") || usage.has("output_tokens");
    }

    private StreamChunk parseStreamChunk(String chunk) {
        if (chunk == null || chunk.isBlank() || "[DONE]".equals(chunk.trim())) {
            return new StreamChunk("", null);
        }
        String payload = chunk.startsWith("data:") ? chunk.substring(5).trim() : chunk.trim();
        if ("[DONE]".equals(payload)) return new StreamChunk("", null);
        try {
            JsonNode event = objectMapper.readTree(payload);
            JsonNode usage = event.path("usage").isObject() ? event.path("usage") : null;
            StringBuilder delta = new StringBuilder();
            JsonNode choices = event.path("choices");
            if (choices.isArray()) {
                for (JsonNode choice : choices) {
                    delta.append(choice.path("delta").path("content").asText(""));
                }
            }
            // Anthropic message stream 的 content_block_delta 使用 delta.text。
            if (delta.isEmpty()) delta.append(event.path("delta").path("text").asText(""));
            return new StreamChunk(delta.toString(), usage);
        } catch (Exception ignored) {
            // 某些本地 OpenAI-compatible 服务直接输出纯文本；此时保留原文本作为估算输入。
            return new StreamChunk(chunk, null);
        }
    }

    private record StreamChunk(String text, JsonNode usage) {
    }

    private long reservePromptTokens(List<ModelEndpoint> endpoints, JsonNode request) {
        return endpoints.stream()
                .mapToLong(endpoint -> tokenEstimator.estimatePromptTokens(endpoint, request))
                .max()
                .orElseGet(() -> tokenEstimator.estimatePromptTokens(request));
    }

    private ReservationPlan reserveUsage(GatewayRequestContext context, List<ModelEndpoint> endpoints, JsonNode request) {
        long promptTokens = reservePromptTokens(endpoints, request);
        OutputTokenPrediction prediction = endpoints.stream()
                .map(endpoint -> outputTokenPredictor.predict(endpoint, request))
                .max(java.util.Comparator.comparingLong(OutputTokenPrediction::selected))
                .orElse(new OutputTokenPrediction(512, 1536, 2048, 4096, 2048, 0, 0, "fallback"));
        BigDecimal estimatedCost = endpoints.stream()
                .map(endpoint -> costCalculator.estimate(endpoint, promptTokens, prediction.selected()))
                .max(BigDecimal::compareTo)
                .orElse(BigDecimal.ZERO);
        if (context.costBudget() != null && estimatedCost.compareTo(context.costBudget()) > 0) {
            throw new GatewayException(HttpStatus.PAYMENT_REQUIRED,
                    "Predicted request cost " + estimatedCost
                            + " exceeds X-Cost-Budget " + context.costBudget());
        }
        UsageReservation reservation = quotaService.reserve(context.userId(), context.requestId(),
                promptTokens, prediction.selected(), estimatedCost);
        return new ReservationPlan(reservation, prediction);
    }

    private record ReservationPlan(UsageReservation reservation, OutputTokenPrediction prediction) {
    }

    /** 发布最终请求 Trace；评估模块通过事件监听解耦，避免网关依赖 Judge 服务形成循环引用。 */
    private void publishTrace(GatewayRequestContext context, JsonNode request, JsonNode response,
                              boolean success, Throwable error) {
        JsonNode safeRequest = request == null ? objectMapper.createObjectNode() : request.deepCopy();
        JsonNode safeResponse = response == null ? objectMapper.createObjectNode() : response.deepCopy();
        BigDecimal cost = decimal(safeResponse.path("gateway").path("costEstimated"));
        String currency = safeResponse.path("gateway").path("costCurrency").asText("");
        eventPublisher.publishEvent(new GatewayTraceEvent(
                context.requestId(), context.traceId(), context.tenantId(), context.userId(),
                context.agentId(), context.agentVersion(), context.sessionId(), context.runId(),
                context.purpose(), context.costBudget(), context.dataRegion(), context.requestedModel(), context.stream(),
                context.startedAt(), Instant.now(), Duration.between(context.startedAt(), Instant.now()).toMillis(),
                safeRequest, safeResponse, success,
                error == null ? "" : error.getClass().getSimpleName(),
                error == null || error.getMessage() == null ? "" : error.getMessage(), cost, currency));
    }

    private BigDecimal decimal(JsonNode node) {
        try {
            return node.isNumber() ? node.decimalValue() : new BigDecimal(node.asText("0"));
        } catch (NumberFormatException ignored) {
            return BigDecimal.ZERO;
        }
    }

    private void logSuccess(GatewayRequestContext context, ModelEndpoint endpoint, GatewayUsage usage,
                            OutputTokenPrediction prediction, Instant started) {
        logSuccess(context, endpoint, usage, prediction, started,
                Duration.between(started, Instant.now()).toMillis());
    }

    private void logSuccess(GatewayRequestContext context, ModelEndpoint endpoint, GatewayUsage usage,
                            OutputTokenPrediction prediction, Instant started, Long ttftMs) {
        long latencyMs = Duration.between(started, Instant.now()).toMillis();
        // TPOT 使用“首 token 之后的剩余耗时 / 剩余输出 token 数”近似计算，
        // 这个口径适合做模型横向比较，不要求和厂商内部采样完全一致。
        long tpotMs = usage.completionTokens() <= 1 ? 0 : Math.max(0, latencyMs - (ttftMs == null ? 0 : ttftMs)) / Math.max(1, usage.completionTokens() - 1);
        BigDecimal tokensPerSecond = latencyMs <= 0
                ? BigDecimal.ZERO
                : BigDecimal.valueOf(usage.completionTokens()).multiply(BigDecimal.valueOf(1000))
                .divide(BigDecimal.valueOf(latencyMs), 4, RoundingMode.HALF_UP);
        log.info("llm_gateway_success requestId={} traceId={} tenant={} user={} agent={} agentVersion={} session={} run={} purpose={} requestedModel={} route={} stream={} totalTokens={} cost={} latencyMs={}",
                context.requestId(), context.traceId(), context.tenantId(), context.userId(),
                context.agentId(), context.agentVersion(), context.sessionId(), context.runId(), context.purpose(),
                context.requestedModel(), endpoint.key(),
                context.stream(), usage.totalTokens(), usage.cost(), latencyMs);
        usageReportService.record(context, endpoint, usage, prediction, latencyMs);
        performanceService.recordSuccess(context, endpoint, usage, latencyMs, ttftMs, tpotMs, tokensPerSecond);
    }
}
