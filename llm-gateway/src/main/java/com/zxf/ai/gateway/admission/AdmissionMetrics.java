package com.zxf.ai.gateway.admission;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 准入控制的低基数 Prometheus 指标。
 *
 * <p>标签只保留阶段、受限维度和原因，绝不把 tenant、user、route 名称作为标签，避免高基数指标拖垮监控系统。</p>
 */
@Component
public class AdmissionMetrics {
    private final MeterRegistry registry;

    /** 注入平台统一的 MeterRegistry，指标由 Actuator Prometheus 端点导出。 */
    public AdmissionMetrics(MeterRegistry registry) {
        this.registry = registry;
    }

    /** 记录一次通过入口或上游准入的请求。 */
    public void allowed(String stage) {
        Counter.builder("llm_gateway_admission_total").tags("outcome", "allowed", "stage", stage).register(registry).increment();
    }

    /** 记录可预期的频率、Token 或并发拒绝。 */
    public void rejected(String stage, String scope, String reason) {
        Counter.builder("llm_gateway_admission_total")
                .tags("outcome", "rejected", "stage", stage, "scope", scope, "reason", reason)
                .register(registry).increment();
    }

    /** 记录 Redis 准入状态不可用，便于区分容量保护和基础设施故障。 */
    public void unavailable(String stage) {
        Counter.builder("llm_gateway_admission_total").tags("outcome", "unavailable", "stage", stage).register(registry).increment();
    }

    /**
     * 记录最近一次实际准入后的最高容量利用率。
     *
     * <p>标签只描述限额类别，不包含租户、用户、路由或模型名称；因此可以用于容量
     * 调优而不会将请求身份写入 Prometheus 的高基数标签空间。</p>
     */
    public void utilization(String stage, String scope, String dimension, double utilization) {
        String key = stage + ':' + scope + ':' + dimension;
        AtomicReference<Double> value = utilizations.computeIfAbsent(key, ignored -> {
            AtomicReference<Double> reference = new AtomicReference<>(0.0);
            Gauge.builder("llm_gateway_admission_utilization", reference, AtomicReference::get)
                    .tags("stage", stage, "scope", scope, "dimension", dimension)
                    .register(registry);
            return reference;
        });
        value.set(Math.max(0.0, Math.min(1.0, utilization)));
    }

    /** 记录一次业务执行入口，用于与上游尝试计数计算调用放大倍数。 */
    public void executionStarted() {
        Counter.builder("llm_gateway_execution_total").register(registry).increment();
    }

    /** 记录一次真正发往模型厂商的尝试；与请求数分离以识别 fallback 放大。 */
    public void upstreamAttempt(String provider) {
        Counter.builder("llm_gateway_upstream_attempt_total")
                .tags("provider", provider).register(registry).increment();
    }

    /** 记录可与本地 429 区分的上游供应商限流事件。 */
    public void providerRateLimited(String provider) {
        Counter.builder("llm_gateway_provider_rate_limited_total")
                .tags("provider", provider).register(registry).increment();
    }

    private final ConcurrentHashMap<String, AtomicReference<Double>> utilizations = new ConcurrentHashMap<>();
}
