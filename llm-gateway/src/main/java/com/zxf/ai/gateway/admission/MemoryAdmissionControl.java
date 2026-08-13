package com.zxf.ai.gateway.admission;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 单机开发和测试使用的准入控制实现。
 *
 * <p>生产多副本必须使用 Redis；本实现故意不尝试跨 JVM 同步状态，以免给出“看似分布式”的错误保证。</p>
 */
@Component
@ConditionalOnProperty(prefix = "gateway.admission", name = "store", havingValue = "memory", matchIfMissing = true)
public class MemoryAdmissionControl implements AdmissionControl {
    private final GatewayProperties.Admission admission;
    private final Map<String, FixedWindow> windows = new ConcurrentHashMap<>();
    private final Map<String, AtomicInteger> inFlight = new ConcurrentHashMap<>();
    private final AdmissionMetrics metrics;

    /** 注入已绑定的准入配置；每个实例独立维护本地窗口和并发计数。 */
    public MemoryAdmissionControl(GatewayProperties properties) {
        this(properties, null);
    }

    /** Spring 装配时注入指标；直接单元测试可省略指标依赖。 */
    @Autowired
    public MemoryAdmissionControl(GatewayProperties properties, AdmissionMetrics metrics) {
        this.admission = properties.getAdmission();
        this.metrics = metrics;
    }

    @Override
    public AdmissionLease admitIngress(GatewayRequestContext context) {
        validateRequestLimits(context);
        return acquire("ingress", List.of(
                Limit.rpm("tenant", context.tenantId(), admission.getTenantRequestsPerMinute()),
                Limit.rpm("user", userKey(context), admission.getUserRequestsPerMinute()),
                Limit.concurrent("tenant", context.tenantId(), admission.getTenantMaxConcurrency()),
                Limit.concurrent("user", userKey(context), admission.getUserMaxConcurrency())
        ));
    }

    @Override
    public AdmissionLease admitUpstream(GatewayRequestContext context, ModelEndpoint endpoint, long estimatedTokens) {
        String route = endpoint.key();
        return acquire("upstream", List.of(
                Limit.rpm("route", route, admission.getRouteRequestsPerMinute()),
                Limit.rpm("provider", endpoint.providerName(), admission.getProviderRequestsPerMinute()),
                Limit.tpm("tenant", context.tenantId(), admission.getTenantTokensPerMinute(), estimatedTokens),
                Limit.tpm("user", userKey(context), admission.getUserTokensPerMinute(), estimatedTokens),
                Limit.tpm("route", route, admission.getRouteTokensPerMinute(), estimatedTokens),
                Limit.tpm("provider", endpoint.providerName(), admission.getProviderTokensPerMinute(), estimatedTokens),
                Limit.concurrent("route", route, admission.getRouteMaxConcurrency()),
                Limit.concurrent("provider", endpoint.providerName(), admission.getProviderMaxConcurrency())
        ));
    }

    @Override
    public void validateRequest(JsonNode request) {
        if (request == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Request body is required");
        }
        int bytes = request.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
        if (bytes > admission.getMaxRequestBytes()) {
            throw new GatewayException(HttpStatus.PAYLOAD_TOO_LARGE, "Request body exceeds configured limit");
        }
        JsonNode messages = request.path("messages");
        if (messages.isArray() && messages.size() > admission.getMaxMessages()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Message count exceeds configured limit");
        }
        long requestedOutput = request.path("max_tokens").asLong(request.path("max_completion_tokens").asLong(0));
        if (requestedOutput > admission.getMaxCompletionTokens()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Requested completion tokens exceed configured limit");
        }
    }

    @Override
    public void validateTokenBounds(long promptTokens, long completionTokens) {
        if (promptTokens > admission.getMaxPromptTokens()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Prompt tokens exceed configured limit");
        }
        if (completionTokens > admission.getMaxCompletionTokens()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Completion tokens exceed configured limit");
        }
    }

    @Override
    public int maxUpstreamAttempts() { return admission.getMaxUpstreamAttempts(); }

    /** 先检查全部速率窗口，再取得并发许可并提交速率消耗；失败时不留下半次准入。 */
    private synchronized AdmissionLease acquire(String stage, List<Limit> limits) {
        List<String> acquiredConcurrent = new ArrayList<>();
        Instant now = Instant.now();
        try {
            for (Limit limit : limits) {
                if (!limit.enabled()) continue;
                if (limit.kind != Kind.CONCURRENT) {
                    FixedWindow window = windows.computeIfAbsent(limit.key(), ignored -> new FixedWindow(now));
                    long retryAfter = window.retryAfter(now, limit.amount, limit.limit);
                    if (retryAfter > 0) throw rejected(limit, retryAfter, window.used(), limit.amount);
                }
            }
            for (Limit limit : limits) {
                if (!limit.enabled()) continue;
                if (limit.kind == Kind.CONCURRENT) {
                    AtomicInteger counter = inFlight.computeIfAbsent(limit.key(), ignored -> new AtomicInteger());
                    if (counter.incrementAndGet() > limit.limit) {
                        counter.decrementAndGet();
                        throw rejected(limit, 1, limit.limit, limit.amount);
                    }
                    acquiredConcurrent.add(limit.key());
                }
            }
            for (Limit limit : limits) {
                if (limit.enabled() && limit.kind != Kind.CONCURRENT) {
                    windows.get(limit.key()).consume(limit.amount);
                }
            }
        } catch (RuntimeException error) {
            acquiredConcurrent.forEach(key -> inFlight.get(key).decrementAndGet());
            if (error instanceof AdmissionRejectedException rejected) {
                recordRejected(stage, rejected.scope(), rejected.reasonCode());
            }
            throw error;
        }
        recordAllowed(stage);
        limits.stream().filter(Limit::enabled)
                .max(java.util.Comparator.comparingDouble(this::utilization))
                .ifPresent(limit -> recordUtilization(stage, limit));
        AtomicBoolean released = new AtomicBoolean();
        return () -> {
            if (released.compareAndSet(false, true)) acquiredConcurrent.forEach(key -> inFlight.get(key).decrementAndGet());
        };
    }

    /** 固定分钟窗口只用于本地；Redis 生产实现使用同一原子脚本来避免多实例竞争。 */
    private static final class FixedWindow {
        private Instant minute;
        private final AtomicLong used = new AtomicLong();

        private FixedWindow(Instant now) { this.minute = now.truncatedTo(java.time.temporal.ChronoUnit.MINUTES); }

        private long retryAfter(Instant now, long amount, long limit) {
            Instant current = now.truncatedTo(java.time.temporal.ChronoUnit.MINUTES);
            if (!current.equals(minute)) { minute = current; used.set(0); }
            if (used.get() + amount > limit) return Math.max(1, Duration.between(now, current.plusSeconds(60)).toSeconds());
            return 0;
        }

        private void consume(long amount) { used.addAndGet(amount); }
        private long used() { return used.get(); }
    }

    private AdmissionRejectedException rejected(Limit limit, long retryAfter, long observedUsage, long requestedAmount) {
        return new AdmissionRejectedException(limit.scope, retryAfter, limit.reasonCode(),
                limit.limit, observedUsage, requestedAmount);
    }

    private void recordAllowed(String stage) { if (metrics != null) metrics.allowed(stage); }
    private void recordRejected(String stage, String scope, String reason) { if (metrics != null) metrics.rejected(stage, scope, reason); }
    private void recordUtilization(String stage, Limit limit) {
        if (metrics != null) metrics.utilization(stage, limit.scope, limit.dimension(), utilization(limit));
    }

    private double utilization(Limit limit) {
        if (limit.kind == Kind.CONCURRENT) {
            return Math.min(1.0, inFlight.getOrDefault(limit.key(), new AtomicInteger()).get() / (double) limit.limit);
        }
        FixedWindow window = windows.get(limit.key());
        return window == null ? 0.0 : Math.min(1.0, window.used() / (double) limit.limit);
    }

    private String userKey(GatewayRequestContext context) { return context.tenantId() + ":" + context.userId(); }

    private void validateRequestLimits(GatewayRequestContext context) {
        if (context.tenantId().isBlank() || context.userId().isBlank()) throw new GatewayException(HttpStatus.UNAUTHORIZED, "Trusted caller identity is required");
    }

    private enum Kind { RPM, TPM, CONCURRENT }

    private record Limit(Kind kind, String scope, String id, long limit, long amount) {
        static Limit rpm(String scope, String id, long limit) { return new Limit(Kind.RPM, scope, id, limit, 1); }
        static Limit tpm(String scope, String id, long limit, long amount) { return new Limit(Kind.TPM, scope, id, limit, Math.max(0, amount)); }
        static Limit concurrent(String scope, String id, long limit) { return new Limit(Kind.CONCURRENT, scope, id, limit, 1); }
        boolean enabled() { return limit > 0; }
        String key() { return kind + ":" + scope + ":" + id; }
        String dimension() { return kind == Kind.CONCURRENT ? "concurrency" : kind.name().toLowerCase(); }
        String reasonCode() { return "ADMISSION_" + scope.toUpperCase() + "_" + dimension().toUpperCase(); }
    }
}
