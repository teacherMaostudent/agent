package com.zxf.ai.gateway.usage;

import java.math.BigDecimal;

/** 调用前的额度预占凭证；结算时以实际用量原子冲正预估 Token 与成本。 */
public record UsageReservation(
        String reservationId,
        long estimatedPromptTokens,
        long estimatedCompletionTokens,
        BigDecimal estimatedCost
) {
    /** 返回本次预占的输入与输出 Token 总量，供结算和释放统一使用。 */
    public long estimatedTotalTokens() {
        return estimatedPromptTokens + estimatedCompletionTokens;
    }
}
