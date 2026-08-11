package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.GatewayUsage;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Duration;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.List;
import java.util.Map;

/**
 * Redis 分布式限额实现。
 *
 * <p>这个实现使用 Redis Lua 脚本把“检查限额 + 扣减 token”放在同一个 Redis 命令里执行，
 * 从而保证多实例并发请求下的原子性。所有实例共享同一组 Redis key，因此适合生产部署。</p>
 */
@Component
@ConditionalOnProperty(prefix = "gateway", name = "quota-store", havingValue = "redis")
public class RedisQuotaService implements QuotaService {
    private static final BigDecimal COST_SCALE = BigDecimal.valueOf(1_000_000);

    private static final DefaultRedisScript<List> RESERVE_SCRIPT = new DefaultRedisScript<>("""
            local tokensKey = KEYS[1]
            local costKey = KEYS[2]
            local reservationKey = KEYS[3]
            local estimatedTokens = tonumber(ARGV[1])
            local estimatedCostMicros = tonumber(ARGV[2])
            local tokenLimit = tonumber(ARGV[3])
            local costLimitMicros = tonumber(ARGV[4])
            local ttlSeconds = tonumber(ARGV[5])

            local currentTokens = tonumber(redis.call('GET', tokensKey) or '0')
            local currentCostMicros = tonumber(redis.call('GET', costKey) or '0')

            if redis.call('EXISTS', reservationKey) == 1 then
                return {'ALREADY_RESERVED', tostring(currentTokens), tostring(currentCostMicros)}
            end

            if currentTokens + estimatedTokens > tokenLimit then
                return {'TOKEN_EXCEEDED', tostring(currentTokens), tostring(currentCostMicros)}
            end

            if currentCostMicros + estimatedCostMicros > costLimitMicros then
                return {'COST_EXCEEDED', tostring(currentTokens), tostring(currentCostMicros)}
            end

            local nextTokens = redis.call('INCRBY', tokensKey, estimatedTokens)
            local nextCostMicros = redis.call('INCRBY', costKey, estimatedCostMicros)
            redis.call('EXPIRE', tokensKey, ttlSeconds)
            redis.call('EXPIRE', costKey, ttlSeconds)
            redis.call('HSET', reservationKey, 'status', 'RESERVED', 'tokens', estimatedTokens, 'costMicros', estimatedCostMicros)
            redis.call('EXPIRE', reservationKey, ttlSeconds)
            return {'OK', tostring(nextTokens), tostring(nextCostMicros)}
            """, List.class);

    private static final DefaultRedisScript<List> RECORD_SCRIPT = new DefaultRedisScript<>("""
            local tokensKey = KEYS[1]
            local costKey = KEYS[2]
            local reservationKey = KEYS[3]
            local tokenDelta = tonumber(ARGV[1])
            local costDeltaMicros = tonumber(ARGV[2])
            local ttlSeconds = tonumber(ARGV[3])
            local finalStatus = ARGV[4]

            if redis.call('HGET', reservationKey, 'status') ~= 'RESERVED' then
                return {'ALREADY_FINALIZED'}
            end

            local nextTokens = redis.call('INCRBY', tokensKey, tokenDelta)
            local nextCostMicros = redis.call('INCRBY', costKey, costDeltaMicros)
            if nextTokens < 0 then redis.call('SET', tokensKey, 0) nextTokens = 0 end
            if nextCostMicros < 0 then redis.call('SET', costKey, 0) nextCostMicros = 0 end
            redis.call('EXPIRE', tokensKey, ttlSeconds)
            redis.call('EXPIRE', costKey, ttlSeconds)
            redis.call('HSET', reservationKey, 'status', finalStatus)
            redis.call('EXPIRE', reservationKey, ttlSeconds)
            return {'OK', tostring(nextTokens), tostring(nextCostMicros)}
            """, List.class);

    private final GatewayProperties properties;
    private final StringRedisTemplate redisTemplate;

    /** 注入共享 Redis 和限额配置，使多网关副本使用同一原子计数边界。 */
    public RedisQuotaService(GatewayProperties properties, StringRedisTemplate redisTemplate) {
        this.properties = properties;
        this.redisTemplate = redisTemplate;
    }

    @Override
    /** 通过 Lua 脚本原子预占当日额度，并以请求标识保证重试幂等。 */
    public UsageReservation reserve(String userId, String requestId, long estimatedPromptTokens,
                                    long estimatedCompletionTokens, BigDecimal estimatedCost) {
        GatewayProperties.UserQuota quota = quota(userId);
        String id = requestId == null || requestId.isBlank() ? java.util.UUID.randomUUID().toString() : requestId;
        List<String> keys = keys(userId, id);
        long estimatedTotalTokens = estimatedPromptTokens + estimatedCompletionTokens;
        List result = redisTemplate.execute(
                RESERVE_SCRIPT,
                keys,
                String.valueOf(estimatedTotalTokens),
                String.valueOf(toMicros(estimatedCost)),
                String.valueOf(quota.getDailyTokenLimit()),
                String.valueOf(toMicros(quota.getDailyCostLimit())),
                String.valueOf(secondsUntilTomorrow())
        );
        String code = result == null || result.isEmpty() ? "UNKNOWN" : String.valueOf(result.get(0));
        if ("TOKEN_EXCEEDED".equals(code)) {
            throw new GatewayException(HttpStatus.TOO_MANY_REQUESTS, "Daily token quota exceeded for user: " + userId);
        }
        if ("COST_EXCEEDED".equals(code)) {
            throw new GatewayException(HttpStatus.TOO_MANY_REQUESTS, "Daily cost quota exceeded for user: " + userId);
        }
        if (!"OK".equals(code) && !"ALREADY_RESERVED".equals(code)) {
            throw new GatewayException(HttpStatus.INTERNAL_SERVER_ERROR, "Redis quota reserve failed: " + code);
        }
        return new UsageReservation(id, estimatedPromptTokens, estimatedCompletionTokens, estimatedCost);
    }

    @Override
    /** 将预占用量与实际账单差额原子记入 Redis，并把预占状态标为已结算。 */
    public void settle(String userId, UsageReservation reservation, GatewayUsage gatewayUsage) {
        redisTemplate.execute(
                RECORD_SCRIPT,
                keys(userId, reservation.reservationId()),
                String.valueOf(gatewayUsage.totalTokens() - reservation.estimatedTotalTokens()),
                String.valueOf(toMicrosSigned(gatewayUsage.cost().subtract(reservation.estimatedCost()))),
                String.valueOf(secondsUntilTomorrow()),
                "SETTLED"
        );
    }

    @Override
    /** 将尚未结算的预占额度原子归还，并标记为已释放以阻止重复扣减。 */
    public void release(String userId, UsageReservation reservation) {
        redisTemplate.execute(
                RECORD_SCRIPT,
                keys(userId, reservation.reservationId()),
                String.valueOf(-reservation.estimatedTotalTokens()),
                String.valueOf(toMicrosSigned(reservation.estimatedCost().negate())),
                String.valueOf(secondsUntilTomorrow()),
                "RELEASED"
        );
    }

    @Override
    /** 读取 Redis 中当前计费日的额度快照，用于监控而非授权。 */
    public Map<String, Object> snapshot(String userId) {
        List<String> keys = keys(userId);
        String tokens = redisTemplate.opsForValue().get(keys.get(0));
        String costMicros = redisTemplate.opsForValue().get(keys.get(1));
        return Map.of(
                "store", "redis",
                "userId", userId,
                "date", LocalDate.now().toString(),
                "tokens", tokens == null ? 0L : Long.parseLong(tokens),
                "cost", fromMicros(costMicros == null ? 0L : Long.parseLong(costMicros))
        );
    }

    /** 生成用户当日 Token 与成本计数键，并对空用户使用匿名隔离命名空间。 */
    private List<String> keys(String userId) {
        String date = LocalDate.now().toString();
        String safeUserId = userId == null || userId.isBlank() ? "anonymous" : userId;
        String prefix = "llm-gateway:quota:" + date + ":" + safeUserId;
        return List.of(prefix + ":tokens", prefix + ":costMicros");
    }

    /** 在用户日限额键后附加经过净化的预占标识，避免键注入。 */
    private List<String> keys(String userId, String reservationId) {
        List<String> quotaKeys = keys(userId);
        String safeReservationId = reservationId == null ? "unknown" : reservationId.replaceAll("[^a-zA-Z0-9._-]", "_");
        return List.of(quotaKeys.get(0), quotaKeys.get(1), quotaKeys.get(0) + ":reservation:" + safeReservationId);
    }

    /** 获取用户定制限额，未配置时使用匿名默认限额。 */
    private GatewayProperties.UserQuota quota(String userId) {
        GatewayProperties.UserQuota quota = properties.getUserQuotas().get(userId);
        if (quota != null) {
            return quota;
        }
        return properties.getUserQuotas().getOrDefault("anonymous", new GatewayProperties.UserQuota());
    }

    /** 计算到下一自然日的最小过期秒数，保证日额度自动滚动。 */
    private long secondsUntilTomorrow() {
        LocalDateTime now = LocalDateTime.now();
        LocalDateTime tomorrow = LocalDateTime.of(LocalDate.now().plusDays(1), LocalTime.MIDNIGHT);
        return Math.max(60, Duration.between(now, tomorrow).toSeconds());
    }

    /** 将非负金额向上转换为微币单位，确保预占不会因舍入而少扣。 */
    private long toMicros(BigDecimal cost) {
        return cost.multiply(COST_SCALE).setScale(0, RoundingMode.CEILING).longValue();
    }

    /** 将可正可负的结算差额转换为微币单位，使用对称舍入规则。 */
    private long toMicrosSigned(BigDecimal cost) {
        return cost.multiply(COST_SCALE).setScale(0, RoundingMode.HALF_UP).longValue();
    }

    /** 将 Redis 整数微币恢复为基础币种的小数金额。 */
    private BigDecimal fromMicros(long micros) {
        return BigDecimal.valueOf(micros).divide(COST_SCALE, 6, RoundingMode.HALF_UP);
    }
}
