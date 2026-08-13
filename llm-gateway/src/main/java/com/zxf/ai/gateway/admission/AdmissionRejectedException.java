package com.zxf.ai.gateway.admission;

import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;

/** 带可执行退避时间的准入拒绝，供 HTTP 层输出 429 和 {@code Retry-After}。 */
public final class AdmissionRejectedException extends GatewayException {
    private final String scope;
    private final long retryAfterSeconds;
    private final String reasonCode;
    private final long configuredLimit;
    private final long observedUsage;
    private final long requestedAmount;

    /** 构造不泄露内部 Redis Key 的拒绝结果，只暴露受限维度和退避秒数。 */
    public AdmissionRejectedException(String scope, long retryAfterSeconds, String reasonCode,
                                      long configuredLimit, long observedUsage, long requestedAmount) {
        super(HttpStatus.TOO_MANY_REQUESTS, "Admission limit exceeded for " + scope + ": " + reasonCode);
        this.scope = scope;
        this.retryAfterSeconds = Math.max(1, retryAfterSeconds);
        this.reasonCode = reasonCode;
        this.configuredLimit = configuredLimit;
        this.observedUsage = observedUsage;
        this.requestedAmount = requestedAmount;
    }

    /** 返回触发限制的公开维度，例如 tenant、route 或 provider。 */
    public String scope() {
        return scope;
    }

    /** 返回调用方应等待的最小整秒数。 */
    public long retryAfterSeconds() {
        return retryAfterSeconds;
    }

    /** 返回稳定的机器可读拒绝码，供调用方重试策略、审计和监控聚合使用。 */
    public String reasonCode() {
        return reasonCode;
    }

    /** 返回已配置的容量上限，不暴露内部限流存储键。 */
    public long configuredLimit() {
        return configuredLimit;
    }

    /** 返回作出拒绝决策时观测到的用量。 */
    public long observedUsage() {
        return observedUsage;
    }

    /** 返回本次请求试图占用的请求数、Token 数或并发槽位数。 */
    public long requestedAmount() {
        return requestedAmount;
    }
}
