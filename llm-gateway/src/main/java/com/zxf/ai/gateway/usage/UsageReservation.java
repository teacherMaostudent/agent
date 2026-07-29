package com.zxf.ai.gateway.usage;

import java.math.BigDecimal;

/** 调用前预留凭证；结算时用实际 usage 对预留 token 和费用做原子冲正。 */
public record UsageReservation(
        String reservationId,
        long estimatedPromptTokens,
        long estimatedCompletionTokens,
        BigDecimal estimatedCost
) {
    public long estimatedTotalTokens() {
        return estimatedPromptTokens + estimatedCompletionTokens;
    }
}
