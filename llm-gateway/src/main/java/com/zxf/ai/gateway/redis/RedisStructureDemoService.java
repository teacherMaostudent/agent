package com.zxf.ai.gateway.redis;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.Map;

@Service
public class RedisStructureDemoService {
    private final StringRedisTemplate redisTemplate;

    public RedisStructureDemoService(ObjectProvider<StringRedisTemplate> redisTemplate) {
        this.redisTemplate = redisTemplate.getIfAvailable();
    }

    /**
     * 用真实 Redis 命令演示常见数据结构在 LLM Gateway 里的落点。
     *
     * <p>String 用作每日额度计数，Hash 用作会话状态，ZSet 用作成本排行榜，
     * Stream 用作轻量异步审计事件。这样 Redis 八股题可以直接回到项目实现。</p>
     */
    public Map<String, Object> demo(String userId, String sessionId) {
        if (redisTemplate == null) {
            return Map.of("enabled", false, "message", "StringRedisTemplate is not available.");
        }

        String safeUser = safe(userId, "demo");
        String safeSession = safe(sessionId, "session");
        String quotaKey = "llm-gateway:demo:string:quota:" + safeUser;
        String sessionKey = "llm-gateway:demo:hash:session:" + safeSession;
        String rankingKey = "llm-gateway:demo:zset:cost-ranking";
        String streamKey = "llm-gateway:demo:stream:audit-events";

        redisTemplate.opsForValue().increment(quotaKey, 1);
        redisTemplate.expire(quotaKey, Duration.ofDays(1));

        redisTemplate.opsForHash().put(sessionKey, "lastModel", "deepseek-v4-flash");
        redisTemplate.opsForHash().put(sessionKey, "state", "ACTIVE");
        redisTemplate.expire(sessionKey, Duration.ofMinutes(30));

        redisTemplate.opsForZSet().incrementScore(rankingKey, safeUser, 0.01);
        redisTemplate.expire(rankingKey, Duration.ofDays(7));

        MapRecord<String, String, String> record = MapRecord.create(streamKey, Map.of(
                "userId", safeUser,
                "event", "ADMIN_REDIS_DEMO",
                "note", "Redis Stream is used as a lightweight async audit/event queue."
        ));
        redisTemplate.opsForStream().add(record);

        return Map.of(
                "enabled", true,
                "string", "Daily quota counter: " + quotaKey,
                "hash", "Session state: " + sessionKey,
                "zset", "Cost ranking: " + rankingKey,
                "stream", "Audit events: " + streamKey,
                "interviewNotes", Map.of(
                        "String", "Counters, quota, idempotency flags.",
                        "Hash", "Session state and compact object fields.",
                        "ZSet", "Rankings, top-N cost users, delayed score windows.",
                        "Stream", "Lightweight async events; Kafka is better for high-throughput durable pipelines."
                )
        );
    }

    /**
     * 将用户输入转换成适合 Redis key 的片段，避免空格、中文或特殊字符污染 key。
     */
    private String safe(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value.replaceAll("[^a-zA-Z0-9_.-]", "_");
    }
}
