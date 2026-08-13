package com.zxf.ai.gateway.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.cache.RequestCacheService;
import com.zxf.ai.gateway.admission.AdmissionControl;
import com.zxf.ai.gateway.admission.AdmissionLease;
import com.zxf.ai.gateway.admission.AdmissionMetrics;
import com.zxf.ai.gateway.admission.AdmissionRejectedException;
import com.zxf.ai.gateway.client.LlmClientRegistry;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayUsage;
import com.zxf.ai.gateway.model.ModelEndpoint;
import com.zxf.ai.gateway.model.ProviderRateLimitedException;
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
import com.zxf.ai.gateway.usage.QuotaExceededException;
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
    private final AdmissionControl admissionControl;
    private final AdmissionMetrics admissionMetrics;

    /** 装配路由、模型客户端、额度、缓存、策略、报表和审计事件等网关协作组件。 */
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
            ApplicationEventPublisher eventPublisher,
            AdmissionControl admissionControl,
            AdmissionMetrics admissionMetrics
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
        this.admissionControl = admissionControl;
        this.admissionMetrics = admissionMetrics;
    }

    /** 执行非流式补全：渲染 Prompt、固定路由版本、命中安全缓存并发布最终 Trace。 */
    public Mono<JsonNode> complete(GatewayRequestContext context, JsonNode request) {
        // 非流式请求可以安全缓存：同一租户、同一请求体命中时直接返回深拷贝结果，
        // 避免重复消耗上游 token。流式请求在 RequestCacheService 中会被跳过。
        /*
         * 先把 prompt_template/template_id + variables 渲染成标准 messages。
         *
         * 这样后续缓存、token 预估、路由、日志和上游调用使用的都是“最终 Prompt”，
         * 避免出现“缓存按模板变量命中，但真实请求按展开后 Prompt 消耗”的口径不一致。
         */
        String routeVersion = request.path("route_version").asText("");
        String modelRevision = request.path("model_revision").asText("");
        JsonNode preparedRequest = promptTemplateService.apply(request);
        if (preparedRequest.isObject()) {
            ((com.fasterxml.jackson.databind.node.ObjectNode) preparedRequest).remove(List.of("route_version", "model_revision"));
        }
        return requestCacheService.cachedOrCompute(context.tenantId(), preparedRequest,
                        () -> completeUncached(context, preparedRequest, routeVersion, modelRevision))
                .doOnSuccess(response -> publishTrace(context, preparedRequest, response, true, null))
                .doOnError(error -> publishTrace(context, preparedRequest, null, false, error));
    }

    /** 为未命中缓存的补全请求建立冻结路由、预算预占和故障回退链路。 */
    private Mono<JsonNode> completeUncached(GatewayRequestContext context, JsonNode request,
            String routeVersion, String modelRevision) {
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
        List<ModelEndpoint> endpoints = boundedEndpoints(modelRouter.resolvePinned(
                request.path("model").asText(context.requestedModel()), routeVersion, modelRevision));
        ReservationPlan plan = reserveUsage(context, endpoints, request);
        return tryComplete(context, request, endpoints, 0, plan, null);
    }

    /** 执行不可整体缓存的流式补全，并在流结束后结算用量和发布审计 Trace。 */
    public Flux<String> stream(GatewayRequestContext context, JsonNode request) {
        /*
         * 流式请求同样先渲染 Prompt 模板。
         * 区别是流式响应不能像普通 JSON 响应一样整体缓存，所以 RequestCacheService 不参与 stream 链路。
         */
        String routeVersion = request.path("route_version").asText("");
        String modelRevision = request.path("model_revision").asText("");
        JsonNode preparedRequest = promptTemplateService.apply(request);
        if (preparedRequest.isObject()) {
            ((com.fasterxml.jackson.databind.node.ObjectNode) preparedRequest).remove(List.of("route_version", "model_revision"));
        }
        List<ModelEndpoint> endpoints = boundedEndpoints(modelRouter.resolvePinned(
                preparedRequest.path("model").asText(context.requestedModel()), routeVersion, modelRevision));
        ReservationPlan plan = reserveUsage(context, endpoints, preparedRequest);
        return tryStream(context, preparedRequest, endpoints, 0, plan, null);
    }

    /** 按调用计划尝试非流式上游端点；仅在失败前未输出时进入下一个回退端点。 */
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
            if (lastError instanceof GatewayException gatewayError
                    && (gatewayError.status() == HttpStatus.TOO_MANY_REQUESTS
                    || gatewayError.status() == HttpStatus.SERVICE_UNAVAILABLE)) {
                // 候选路由都不可用时保留 429/503，而不是把可退避的事实误包装为 502。
                return Mono.error(gatewayError);
            }
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

        AdmissionLease lease;
        try {
            lease = admissionControl.admitUpstream(context, endpoint, plan.reservation().estimatedTotalTokens());
        } catch (GatewayException ex) {
            // 受限路由不应拖垮其他候选；若所有候选都拒绝，末端会保留最后的 429/503 语义。
            return tryComplete(context, request, endpoints, index + 1, plan, ex);
        }

        admissionMetrics.upstreamAttempt(endpoint.providerName());
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
                    if (error instanceof ProviderRateLimitedException limited) {
                        admissionMetrics.providerRateLimited(limited.provider());
                    }
                    performanceService.recordFailure(context, endpoint, error, Duration.between(started, Instant.now()).toMillis());
                    log.warn("llm_gateway_fallback requestId={} user={} failedRoute={} reason={}",
                            context.requestId(), context.userId(), endpoint.key(), error.getMessage());
                    return tryComplete(context, request, endpoints, index + 1, plan, error);
                })
                // 此许可只包裹一次真实 upstream attempt；fallback 会重新申请自身 route/provider 许可。
                .doFinally(ignored -> lease.release());
    }

    /** 截断 fallback 调用计划，避免故障期将一次业务请求放大为无界上游流量。 */
    private List<ModelEndpoint> boundedEndpoints(List<ModelEndpoint> endpoints) {
        int maximum = Math.max(1, admissionControl.maxUpstreamAttempts());
        return endpoints.size() <= maximum ? endpoints : endpoints.subList(0, maximum);
    }

    /** 按调用计划尝试流式上游端点；响应首块后失败不得切换端点以免混合输出。 */
    private Flux<String> tryStream(
            GatewayRequestContext context,
            JsonNode request,
            List<ModelEndpoint> endpoints,
            int index,
            ReservationPlan plan,
            Throwable lastError
    ) {
        if (index >= endpoints.size()) {
            quotaService.release(context.userId(), plan.reservation());
            if (lastError instanceof GatewayException gatewayError
                    && (gatewayError.status() == HttpStatus.TOO_MANY_REQUESTS
                    || gatewayError.status() == HttpStatus.SERVICE_UNAVAILABLE)) {
                return Flux.error(gatewayError);
            }
            return Flux.error(new GatewayException(HttpStatus.BAD_GATEWAY, "All model routes failed"));
        }

        ModelEndpoint endpoint = endpoints.get(index);
        Instant started = Instant.now();
        if (!policyService.isAvailable(endpoint)) {
            return tryStream(context, request, endpoints, index + 1, plan,
                    new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "Circuit is open for route: " + endpoint.key()));
        }
        try {
            policyService.beforeCall(endpoint);
        } catch (GatewayException ex) {
            return tryStream(context, request, endpoints, index + 1, plan, ex);
        }

        AdmissionLease lease;
        try {
            lease = admissionControl.admitUpstream(context, endpoint, plan.reservation().estimatedTotalTokens());
        } catch (GatewayException ex) {
            return tryStream(context, request, endpoints, index + 1, plan, ex);
        }

        AtomicReference<StringBuilder> text = new AtomicReference<>(new StringBuilder());
        AtomicLong completionTokens = new AtomicLong();
        AtomicReference<Long> ttftMs = new AtomicReference<>();
        AtomicReference<JsonNode> reportedUsage = new AtomicReference<>();
        AtomicBoolean responseStarted = new AtomicBoolean();
        admissionMetrics.upstreamAttempt(endpoint.providerName());
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
                    if (error instanceof ProviderRateLimitedException limited) {
                        admissionMetrics.providerRateLimited(limited.provider());
                    }
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
                    return tryStream(context, request, endpoints, index + 1, plan, error);
                })
                .doOnError(error -> {
                    if (index == 0) publishTrace(context, request, null, false, error);
                })
                .doFinally(ignored -> lease.release());
    }

    /** 在兼容上游响应中附加可审计的路由、用量、成本与输出长度预测元数据。 */
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

    /** 优先读取供应商输出 Token；缺失时根据返回内容按端点分词策略补算。 */
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

    /** 从完整响应的 Usage 中读取提示词 Token，缺失时使用预先估算值。 */
    private long promptTokens(JsonNode response, long fallback) {
        return promptTokensFromUsage(response.path("usage"), fallback);
    }

    /** 兼容 OpenAI 与 Anthropic 输入字段，并将缓存读写 Token 纳入实际提示词用量。 */
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

    /** 兼容不同供应商的输出 Token 字段，缺失遥测时回退到局部估算。 */
    private long completionTokensFromUsage(JsonNode usage, long fallback) {
        if (usage.has("completion_tokens")) return usage.path("completion_tokens").asLong();
        if (usage.has("output_tokens")) return usage.path("output_tokens").asLong();
        return fallback;
    }

    /** 判断响应是否携带可用于正式结算的任一供应商 Usage 字段。 */
    private boolean hasProviderUsage(JsonNode response) {
        JsonNode usage = response.path("usage");
        return usage.has("prompt_tokens") || usage.has("input_tokens")
                || usage.has("completion_tokens") || usage.has("output_tokens");
    }

    /** 解析 OpenAI 或 Anthropic 流事件中的文本增量和可选 Usage；无法解析时保留原始文本。 */
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

    /** 表示单个上游流事件解析出的文本增量及可选供应商用量。 */
    private record StreamChunk(String text, JsonNode usage) {
    }

    /** 取所有候选端点中最大的提示词估算，保证故障回退不会突破已预占额度。 */
    private long reservePromptTokens(List<ModelEndpoint> endpoints, JsonNode request) {
        return endpoints.stream()
                .mapToLong(endpoint -> tokenEstimator.estimatePromptTokens(endpoint, request))
                .max()
                .orElseGet(() -> tokenEstimator.estimatePromptTokens(request));
    }

    /** 以最坏候选端点成本预占配额，并先拒绝超过调用方成本预算的请求。 */
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
        admissionControl.validateTokenBounds(promptTokens, prediction.selected());
        if (context.costBudget() != null && estimatedCost.compareTo(context.costBudget()) > 0) {
            throw new GatewayException(HttpStatus.PAYMENT_REQUIRED,
                    "Predicted request cost " + estimatedCost
                            + " exceeds X-Cost-Budget " + context.costBudget());
        }
        UsageReservation reservation = quotaService.reserve(context.userId(), context.requestId(),
                promptTokens, prediction.selected(), estimatedCost);
        return new ReservationPlan(reservation, prediction);
    }

    /** 将额度预占凭证与本次选择的输出预测绑定，防止结算口径漂移。 */
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
                error == null || error.getMessage() == null ? "" : error.getMessage(), cost, currency,
                decisionCode(error), decisionScope(error), retryAfterSeconds(error)));
    }

    /** 将内部准入、配额与上游限流统一投影为不依赖异常类名的审计原因码。 */
    private String decisionCode(Throwable error) {
        if (error instanceof AdmissionRejectedException rejected) return rejected.reasonCode();
        if (error instanceof ProviderRateLimitedException limited) return limited.reasonCode();
        if (error instanceof QuotaExceededException exceeded) return exceeded.reasonCode();
        return "";
    }

    /** 只导出公共限流维度，不把租户、用户或 Redis Key 写进治理事件。 */
    private String decisionScope(Throwable error) {
        if (error instanceof AdmissionRejectedException rejected) return rejected.scope();
        if (error instanceof ProviderRateLimitedException limited) return "provider";
        if (error instanceof QuotaExceededException) return "daily-quota";
        return "";
    }

    /** 为可退避的错误导出秒级提示；普通业务错误保持为零。 */
    private long retryAfterSeconds(Throwable error) {
        if (error instanceof AdmissionRejectedException rejected) return rejected.retryAfterSeconds();
        if (error instanceof ProviderRateLimitedException limited) return limited.retryAfterSeconds();
        return 0;
    }

    /** 将网关响应中的成本字段安全转为小数，非法或缺失值视为零。 */
    private BigDecimal decimal(JsonNode node) {
        try {
            return node.isNumber() ? node.decimalValue() : new BigDecimal(node.asText("0"));
        } catch (NumberFormatException ignored) {
            return BigDecimal.ZERO;
        }
    }

    /** 为非流式调用记录完整成功指标，并以总延迟近似首 Token 时间。 */
    private void logSuccess(GatewayRequestContext context, ModelEndpoint endpoint, GatewayUsage usage,
                            OutputTokenPrediction prediction, Instant started) {
        logSuccess(context, endpoint, usage, prediction, started,
                Duration.between(started, Instant.now()).toMillis());
    }

    /** 记录成功调用的审计日志、用量报表和性能指标，流式场景保留真实 TTFT。 */
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
