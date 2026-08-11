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

    /** 注入本地演示模式的用户限额配置；生产多副本应使用 Redis 实现。 */
    public MemoryQuotaService(GatewayProperties properties) {
        this.properties = properties;
    }

    @Override
    /** 原子预占估算 Token 与费用，防止单 JVM 内并发请求突破日限额。 */
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
    /** 用实际用量结算预占额度；重复结算不会再次修改计数器。 */
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
    /** 上游未产生可计费用量时释放预占额度；重复释放保持幂等。 */
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
    /** 返回当前 JVM 的诊断快照，不可将其视为分布式授权依据。 */
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

    /** 获取当天用户计数器；跨日时原子替换为新的零值计数器。 */
    private UsageCounter counter(String userId) {
        return usage.compute(userId, (key, existing) -> {
            if (existing == null || !existing.date.equals(LocalDate.now())) {
                return new UsageCounter(LocalDate.now());
            }
            return existing;
        });
    }

    /** 读取用户专属限额，缺失时回退至匿名用户的默认限额。 */
    private GatewayProperties.UserQuota quota(String userId) {
        GatewayProperties.UserQuota quota = properties.getUserQuotas().get(userId);
        if (quota != null) {
            return quota;
        }
        return properties.getUserQuotas().getOrDefault("anonymous", new GatewayProperties.UserQuota());
    }

    /** 修正结算或释放后的负数，避免展示和后续校验出现负用量。 */
    private void normalize(UsageCounter counter) {
        if (counter.tokens.get() < 0) counter.tokens.set(0);
        if (counter.cost.signum() < 0) counter.cost = BigDecimal.ZERO;
    }

    /** 构造用户和请求唯一的预占键，隔离不同用户的重复请求。 */
    private String reservationKey(String userId, String reservationId) {
        return (userId == null ? "anonymous" : userId) + ":" + reservationId;
    }

    private static class UsageCounter {
        private final LocalDate date;
        private final AtomicLong tokens = new AtomicLong();
        private BigDecimal cost = BigDecimal.ZERO;

        /** 创建只属于一个计费日的可同步用量计数器。 */
        private UsageCounter(LocalDate date) {
            this.date = date;
        }
    }
}
