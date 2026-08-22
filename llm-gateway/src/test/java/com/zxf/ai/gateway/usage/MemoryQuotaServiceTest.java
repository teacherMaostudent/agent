package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class MemoryQuotaServiceTest {
    @Test
    void reserveAndSettleShouldReplacePredictionWithActualUsage() {
        GatewayProperties properties = new GatewayProperties();
        GatewayProperties.UserQuota quota = new GatewayProperties.UserQuota();
        quota.setDailyTokenLimit(10);
        properties.setUserQuotas(Map.of("demo", quota));
        MemoryQuotaService quotaService = new MemoryQuotaService(properties);

        UsageReservation reservation = quotaService.reserve("demo", "req-1", 3, 3, new BigDecimal("0.60"));

        assertThat(quotaService.snapshot("demo")).containsEntry("tokens", 6L);
        quotaService.settle("demo", reservation,
                com.zxf.ai.gateway.model.GatewayUsage.of(3, 1, new BigDecimal("0.25")));
        // Reactor 重放或网络重试导致重复结算时，第二次必须是幂等 no-op。
        quotaService.settle("demo", reservation,
                com.zxf.ai.gateway.model.GatewayUsage.of(3, 1, new BigDecimal("0.25")));
        assertThat(quotaService.snapshot("demo"))
                .containsEntry("tokens", 4L)
                .containsEntry("cost", new BigDecimal("0.25"));

        assertThatThrownBy(() -> quotaService.reserve("demo", "req-2", 7, 0, BigDecimal.ZERO))
                .isInstanceOf(GatewayException.class)
                .hasMessageContaining("Daily token quota exceeded");
    }

    @Test
    void releaseShouldReturnReservedTokensAndCost() {
        GatewayProperties properties = new GatewayProperties();
        MemoryQuotaService quotaService = new MemoryQuotaService(properties);
        UsageReservation reservation = quotaService.reserve("demo", "req-release", 10, 20, new BigDecimal("0.50"));

        quotaService.release("demo", reservation);
        quotaService.release("demo", reservation);

        Map<String, Object> snapshot = quotaService.snapshot("demo");
        assertThat(snapshot).containsEntry("tokens", 0L);
        assertThat((BigDecimal) snapshot.get("cost")).isEqualByComparingTo(BigDecimal.ZERO);
    }

    @Test
    void sameUserIdMustHaveIndependentCountersAcrossTenants() {
        GatewayProperties properties = new GatewayProperties();
        MemoryQuotaService quotaService = new MemoryQuotaService(properties);

        quotaService.reserve("tenant-a", "shared-user", "req-1", 10, 5, new BigDecimal("0.20"));

        assertThat(quotaService.snapshot("tenant-a", "shared-user"))
                .containsEntry("tokens", 15L);
        assertThat(quotaService.snapshot("tenant-b", "shared-user"))
                .containsEntry("tokens", 0L);
    }
}
