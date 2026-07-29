package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayUsage;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 单实例内存限额实现。
 *
 * <p>这个实现适合本地开发和演示。它只能保证当前 JVM 内的并发安全，
 * 多实例部署时每个实例都有自己的内存计数，会导致全局限额不准确。
 * 生产环境应将 `gateway.quota-store` 设置为 `redis`。</p>
 */
@Component
@ConditionalOnProperty(prefix = "gateway", name = "quota-store", havingValue = "memory", matchIfMissing = true)
public class MemoryQuotaService implements QuotaService {
    private final GatewayProperties properties;
    private final Map<String, UsageCounter> usage = new ConcurrentHashMap<>();
    private final Map<String, UsageReservation> activeReservations = new ConcurrentHashMap<>();

    public MemoryQuotaService(GatewayProperties properties) {
        this.properties = properties;
    }

    @Override
    public UsageReservation reserve(String userId, String requestId, long estimatedPromptTokens,
                                    long estimatedCompletionTokens, BigDecimal estimatedCost) {
        UsageCounter counter = counter(userId);
        GatewayProperties.UserQuota quota = quota(userId);
        long estimatedTotalTokens = estimatedPromptTokens + estimatedCompletionTokens;
        String id = requestId == null || requestId.isBlank() ? UUID.randomUUID().toString() : requestId;
        String reservationKey = reservationKey(userId, id);
        UsageReservation reservation = new UsageReservation(id, estimatedPromptTokens,
                estimatedCompletionTokens, estimatedCost);

        synchronized (counter) {
            UsageReservation existing = activeReservations.get(reservationKey);
            if (existing != null) return existing;
            long nextTokens = counter.tokens.get() + estimatedTotalTokens;
            if (nextTokens > quota.getDailyTokenLimit()) {
                throw new GatewayException(HttpStatus.TOO_MANY_REQUESTS, "Daily token quota exceeded for user: " + userId);
            }
            if (counter.cost.add(estimatedCost).compareTo(quota.getDailyCostLimit()) > 0) {
                throw new GatewayException(HttpStatus.TOO_MANY_REQUESTS, "Daily cost quota exceeded for user: " + userId);
            }
            counter.tokens.addAndGet(estimatedTotalTokens);
            counter.cost = counter.cost.add(estimatedCost);
            activeReservations.put(reservationKey, reservation);
        }
        return reservation;
    }

    @Override
    public void settle(String userId, UsageReservation reservation, GatewayUsage gatewayUsage) {
        UsageCounter counter = counter(userId);
        synchronized (counter) {
            if (activeReservations.remove(reservationKey(userId, reservation.reservationId())) == null) return;
            counter.tokens.addAndGet(gatewayUsage.totalTokens() - reservation.estimatedTotalTokens());
            counter.cost = counter.cost.add(gatewayUsage.cost().subtract(reservation.estimatedCost()));
            normalize(counter);
        }
    }

    @Override
    public void release(String userId, UsageReservation reservation) {
        UsageCounter counter = counter(userId);
        synchronized (counter) {
            if (activeReservations.remove(reservationKey(userId, reservation.reservationId())) == null) return;
            counter.tokens.addAndGet(-reservation.estimatedTotalTokens());
            counter.cost = counter.cost.subtract(reservation.estimatedCost());
            normalize(counter);
        }
    }

    @Override
    public Map<String, Object> snapshot(String userId) {
        UsageCounter counter = counter(userId);
        return Map.of(
                "store", "memory",
                "userId", userId,
                "date", counter.date.toString(),
                "tokens", counter.tokens.get(),
                "cost", counter.cost
        );
    }

    private UsageCounter counter(String userId) {
        return usage.compute(userId, (key, existing) -> {
            if (existing == null || !existing.date.equals(LocalDate.now())) {
                return new UsageCounter(LocalDate.now());
            }
            return existing;
        });
    }

    private GatewayProperties.UserQuota quota(String userId) {
        GatewayProperties.UserQuota quota = properties.getUserQuotas().get(userId);
        if (quota != null) {
            return quota;
        }
        return properties.getUserQuotas().getOrDefault("anonymous", new GatewayProperties.UserQuota());
    }

    private void normalize(UsageCounter counter) {
        if (counter.tokens.get() < 0) counter.tokens.set(0);
        if (counter.cost.signum() < 0) counter.cost = BigDecimal.ZERO;
    }

    private String reservationKey(String userId, String reservationId) {
        return (userId == null ? "anonymous" : userId) + ":" + reservationId;
    }

    private static class UsageCounter {
        private final LocalDate date;
        private final AtomicLong tokens = new AtomicLong();
        private BigDecimal cost = BigDecimal.ZERO;

        private UsageCounter(LocalDate date) {
            this.date = date;
        }
    }
}
