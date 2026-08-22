package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.model.GatewayUsage;

import java.math.BigDecimal;
import java.util.Map;

/** 模型用量的分布式限额边界；实现必须让预占操作具备原子性。 */
public interface QuotaService {
    /** 在访问上游模型前原子预占估算成本与 Token，防止多副本超卖额度。 */
    UsageReservation reserve(String tenantId, String userId, String requestId, long estimatedPromptTokens,
                             long estimatedCompletionTokens, BigDecimal estimatedCost);

    /** 成功后以供应商报告或本地估算的实际用量结算预占额度。 */
    void settle(String tenantId, String userId, UsageReservation reservation, GatewayUsage gatewayUsage);

    /** 未产生可计费上游结果时释放预占额度，供故障回退和最终失败路径调用。 */
    void release(String tenantId, String userId, UsageReservation reservation);

    /** 返回运维快照，不承担身份认证或访问授权决策。 */
    Map<String, Object> snapshot(String tenantId, String userId);

    /** Compatibility overloads keep local unit tests and old in-process callers tenant-scoped. */
    default UsageReservation reserve(String userId, String requestId, long prompt, long completion,
                                     BigDecimal cost) {
        return reserve("default", userId, requestId, prompt, completion, cost);
    }

    default void settle(String userId, UsageReservation reservation, GatewayUsage usage) {
        settle("default", userId, reservation, usage);
    }

    default void release(String userId, UsageReservation reservation) {
        release("default", userId, reservation);
    }

    default Map<String, Object> snapshot(String userId) {
        return snapshot("default", userId);
    }
}
