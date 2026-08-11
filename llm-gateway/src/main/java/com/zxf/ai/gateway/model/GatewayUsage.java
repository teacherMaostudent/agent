package com.zxf.ai.gateway.model;

import java.math.BigDecimal;

/**
 * 单次模型调用的用量统计结果。
 *
 * <p>promptTokens 来自请求侧估算，completionTokens 优先来自上游 usage，
 * cost 由模型单价和 token 数计算得到。</p>
 */
public record GatewayUsage(
        long promptTokens,
        long completionTokens,
        long totalTokens,
        BigDecimal cost,
        String currency,
        String usageSource,
        String costStatus
) {
    /**
     * 统一创建用量对象，避免调用方重复计算 totalTokens。
     */
    public static GatewayUsage of(long promptTokens, long completionTokens, BigDecimal cost) {
        return of(promptTokens, completionTokens, cost, "", "LOCAL_TOKENIZER", "ESTIMATED");
    }

    /** 用完整的币种、使用量来源和成本状态创建可审计的用量记录。 */
    public static GatewayUsage of(long promptTokens, long completionTokens, BigDecimal cost,
                                  String currency, String usageSource, String costStatus) {
        return new GatewayUsage(promptTokens, completionTokens, promptTokens + completionTokens,
                cost, currency, usageSource, costStatus);
    }
}
