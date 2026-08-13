package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;

import java.math.BigDecimal;

/**
 * 日配额拒绝的结构化错误。
 *
 * <p>配额与瞬时准入都可能表现为 HTTP 429，但配额不会因为等待数秒自动恢复；调用方、
 * 审计和告警必须能凭原因码将其与 RPM/TPM 拒绝分开。</p>
 */
public final class QuotaExceededException extends GatewayException {
    private final String reasonCode;
    private final BigDecimal configuredLimit;
    private final BigDecimal observedUsage;
    private final BigDecimal requestedAmount;

    /** 创建不暴露用户身份的日 Token 或成本配额拒绝。 */
    public QuotaExceededException(String reasonCode, BigDecimal configuredLimit,
                                  BigDecimal observedUsage, BigDecimal requestedAmount) {
        super(HttpStatus.TOO_MANY_REQUESTS, "QUOTA_DAILY_TOKEN".equals(reasonCode)
                ? "Daily token quota exceeded" : "Daily cost quota exceeded");
        this.reasonCode = reasonCode;
        this.configuredLimit = configuredLimit;
        this.observedUsage = observedUsage;
        this.requestedAmount = requestedAmount;
    }

    /** 返回 QUOTA_DAILY_TOKEN 或 QUOTA_DAILY_COST。 */
    public String reasonCode() { return reasonCode; }
    /** 返回日配额上限。 */
    public BigDecimal configuredLimit() { return configuredLimit; }
    /** 返回当前已记账用量。 */
    public BigDecimal observedUsage() { return observedUsage; }
    /** 返回本次申请的预计用量。 */
    public BigDecimal requestedAmount() { return requestedAmount; }
}
