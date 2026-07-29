package com.zxf.ai.gateway.resilience;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

@Component
public class GatewayPolicyService {
    private static final String ROUTE_STATE_KIND = "route-policy-state";

    private final GatewayProperties properties;
    private final RuntimeStateRepository stateRepository;
    /**
     * routeKey -> 运行态健康状态。
     * 当前状态保存在单机内存；多实例部署时可同步到 Redis 或治理平台。
     */
    private final Map<String, RouteState> states = new ConcurrentHashMap<>();
    /**
     * 简单分钟桶限流计数器。
     * 生产环境建议增加后台清理或使用 Redis TTL，避免长期运行后桶数量持续增长。
     */
    private final Map<String, AtomicInteger> minuteCounters = new ConcurrentHashMap<>();

    public GatewayPolicyService(GatewayProperties properties, ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.properties = properties;
        this.stateRepository = stateRepository.getIfAvailable();
    }

    public void beforeCall(ModelEndpoint endpoint) {
        RouteState state = state(endpoint.key());
        if (state.openUntil != null && state.openUntil.isAfter(Instant.now())) {
            // 熔断窗口内快速失败，让上层 fallback 到其他模型，避免继续打故障上游。
            throw new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "Circuit is open for route: " + endpoint.key());
        }
        String bucket = endpoint.key() + ":" + Instant.now().truncatedTo(ChronoUnit.MINUTES);
        int count = minuteCounters.computeIfAbsent(bucket, key -> new AtomicInteger()).incrementAndGet();
        if (count > properties.getResilience().getRouteRateLimitPerMinute()) {
            // 限流按路由维度生效，避免某个模型被单个入口打满。
            throw new GatewayException(HttpStatus.TOO_MANY_REQUESTS, "Route rate limit exceeded: " + endpoint.key());
        }
    }

    public void recordSuccess(ModelEndpoint endpoint) {
        RouteState state = state(endpoint.key());
        // 成功一次即可关闭熔断并清零连续失败计数，属于简单半开恢复策略。
        state.failureCount.set(0);
        state.openUntil = null;
        state.healthy = true;
        state.lastSuccessAt = Instant.now();
        persist(endpoint.key(), state);
    }

    public void recordFailure(ModelEndpoint endpoint) {
        RouteState state = state(endpoint.key());
        state.healthy = false;
        state.lastFailureAt = Instant.now();
        int failures = state.failureCount.incrementAndGet();
        if (failures >= properties.getResilience().getCircuitFailureThreshold()) {
            // 连续失败达到阈值后进入 open 状态，直到 openUntil 之后才允许再次尝试。
            state.openUntil = Instant.now().plus(properties.getResilience().getCircuitOpenDuration());
        }
        persist(endpoint.key(), state);
    }

    public boolean isAvailable(ModelEndpoint endpoint) {
        RouteState state = state(endpoint.key());
        return state.openUntil == null || state.openUntil.isBefore(Instant.now());
    }

    public Map<String, Object> snapshot() {
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "routes", states,
                "rateLimitPerMinute", properties.getResilience().getRouteRateLimitPerMinute(),
                "circuitFailureThreshold", properties.getResilience().getCircuitFailureThreshold()
        );
    }

    private RouteState state(String key) {
        return states.computeIfAbsent(key, ignored -> {
            RouteState state = new RouteState();
            if (stateRepository != null) {
                stateRepository.findDocument(ROUTE_STATE_KIND, key, RouteStateDocument.class)
                        .ifPresent(state::apply);
            }
            return state;
        });
    }

    private void persist(String key, RouteState state) {
        if (stateRepository != null) {
            stateRepository.saveDocument(ROUTE_STATE_KIND, key, RouteStateDocument.from(key, state));
        }
    }

    public static class RouteState {
        private final AtomicInteger failureCount = new AtomicInteger();
        private volatile boolean healthy = true;
        private volatile Instant openUntil;
        private volatile Instant lastSuccessAt;
        private volatile Instant lastFailureAt;

        public int getFailureCount() {
            return failureCount.get();
        }

        public boolean isHealthy() {
            return healthy;
        }

        public Instant getOpenUntil() {
            return openUntil;
        }

        public Instant getLastSuccessAt() {
            return lastSuccessAt;
        }

        public Instant getLastFailureAt() {
            return lastFailureAt;
        }

        private void apply(RouteStateDocument document) {
            failureCount.set(document.failureCount());
            healthy = document.healthy();
            openUntil = document.openUntil();
            lastSuccessAt = document.lastSuccessAt();
            lastFailureAt = document.lastFailureAt();
        }
    }

    public record RouteStateDocument(
            String routeKey,
            int failureCount,
            boolean healthy,
            Instant openUntil,
            Instant lastSuccessAt,
            Instant lastFailureAt
    ) {
        static RouteStateDocument from(String routeKey, RouteState state) {
            return new RouteStateDocument(
                    routeKey,
                    state.getFailureCount(),
                    state.isHealthy(),
                    state.getOpenUntil(),
                    state.getLastSuccessAt(),
                    state.getLastFailureAt()
            );
        }
    }
}
