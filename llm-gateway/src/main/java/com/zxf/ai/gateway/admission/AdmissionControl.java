package com.zxf.ai.gateway.admission;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.ModelEndpoint;

/**
 * 模型调用前的统一准入边界。
 *
 * <p>它只处理请求频率、Token 吞吐、并发和输入大小；每日成本/Token 配额仍由
 * {@code QuotaService} 负责，熔断仍由 {@code GatewayPolicyService} 负责。分离这些职责能避免
 * 把短期过载、长期预算和上游故障混成同一种 429。</p>
 */
public interface AdmissionControl {
    /** 在缓存命中前限制租户和用户的入口请求频率，并返回必须在响应结束时释放的并发租约。 */
    AdmissionLease admitIngress(GatewayRequestContext context);

    /** 在每一次真实上游调用前限制 TPM、路由/供应商 RPM 与并发，并返回该尝试的资源租约。 */
    AdmissionLease admitUpstream(GatewayRequestContext context, ModelEndpoint endpoint, long estimatedTokens);

    /** 拒绝过大的 JSON 载荷、消息数量或客户端声明的最大输出，避免限流器被异常请求绕过。 */
    void validateRequest(JsonNode request);

    /** 拒绝超出单次输入或预计输出 Token 上限的请求。 */
    void validateTokenBounds(long promptTokens, long completionTokens);

    /** 返回单次用户请求可以触发的最大真实上游尝试数。 */
    int maxUpstreamAttempts();
}
