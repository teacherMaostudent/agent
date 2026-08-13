package com.zxf.ai.gateway.admission;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 多副本生产环境的 Redis 准入控制。
 *
 * <p>同一个 Lua 脚本会先检查全部速率/Token/并发维度、再一次性扣减，避免“前几个维度已消费、
 * 后一个维度拒绝”造成配额泄漏。速率使用带有限突发容量的 Token Bucket，消除了固定分钟桶边界突刺。</p>
 */
@Component
@ConditionalOnProperty(prefix = "gateway.admission", name = "store", havingValue = "redis")
public class RedisAdmissionControl implements AdmissionControl {
    private static final DefaultRedisScript<List> ADMIT_SCRIPT = new DefaultRedisScript<>("""
            local count = tonumber(ARGV[1])
            local now = tonumber(ARGV[2])
            local burstSeconds = tonumber(ARGV[3])
            local leaseTtl = tonumber(ARGV[4])
            local maxUtilization = 0
            local maxIndex = 0
            for i = 1, count do
              local base = 5 + (i - 1) * 3
              local kind = ARGV[base]
              local limit = tonumber(ARGV[base + 1])
              local amount = tonumber(ARGV[base + 2])
              if kind == 'RATE' then
                local rawTokens = redis.call('HGET', KEYS[i], 'tokens')
                local rawUpdated = redis.call('HGET', KEYS[i], 'updatedAt')
                local capacity = math.max(1, limit * burstSeconds / 60)
                local tokens = rawTokens and tonumber(rawTokens) or capacity
                local updatedAt = rawUpdated and tonumber(rawUpdated) or now
                tokens = math.min(capacity, tokens + math.max(0, now - updatedAt) * limit / 60000)
                if tokens < amount then
                  local waitMs = math.ceil((amount - tokens) * 60000 / limit)
                  return {'REJECT', tostring(i), 'RATE', tostring(math.max(1, math.ceil(waitMs / 1000))), tostring(capacity - tokens), tostring(amount)}
                end
              else
                local current = tonumber(redis.call('GET', KEYS[i]) or '0')
                if current >= limit then return {'REJECT', tostring(i), 'CONCURRENCY', '1', tostring(current), tostring(amount)} end
              end
            end
            for i = 1, count do
              local base = 5 + (i - 1) * 3
              local kind = ARGV[base]
              local limit = tonumber(ARGV[base + 1])
              local amount = tonumber(ARGV[base + 2])
              if kind == 'RATE' then
                local rawTokens = redis.call('HGET', KEYS[i], 'tokens')
                local rawUpdated = redis.call('HGET', KEYS[i], 'updatedAt')
                local capacity = math.max(1, limit * burstSeconds / 60)
                local tokens = rawTokens and tonumber(rawTokens) or capacity
                local updatedAt = rawUpdated and tonumber(rawUpdated) or now
                tokens = math.min(capacity, tokens + math.max(0, now - updatedAt) * limit / 60000) - amount
                redis.call('HSET', KEYS[i], 'tokens', tokens, 'updatedAt', now)
                redis.call('PEXPIRE', KEYS[i], math.max(120000, math.ceil(capacity * 60000 / limit) + 60000))
                local utilization = (capacity - tokens) / capacity
                if utilization > maxUtilization then maxUtilization = utilization maxIndex = i end
              else
                local current = redis.call('INCRBY', KEYS[i], amount)
                redis.call('EXPIRE', KEYS[i], leaseTtl)
                local utilization = current / limit
                if utilization > maxUtilization then maxUtilization = utilization maxIndex = i end
              end
            end
            return {'OK', tostring(maxIndex), tostring(maxUtilization)}
            """, List.class);

    private static final DefaultRedisScript<Long> RELEASE_SCRIPT = new DefaultRedisScript<>("""
            for i = 1, #KEYS do
              local current = tonumber(redis.call('GET', KEYS[i]) or '0')
              if current <= 1 then redis.call('DEL', KEYS[i]) else redis.call('DECR', KEYS[i]) end
            end
            return 1
            """, Long.class);

    private final GatewayProperties.Admission admission;
    private final StringRedisTemplate redis;
    private final AdmissionMetrics metrics;

    /** 注入共享 Redis；所有 Gateway 副本因此共享同一个准入事实。 */
    public RedisAdmissionControl(GatewayProperties properties, StringRedisTemplate redis) {
        this(properties, redis, null);
    }

    /** Spring 装配时注入低基数指标；显式构造保留给 Redis 集成测试。 */
    @Autowired
    public RedisAdmissionControl(GatewayProperties properties, StringRedisTemplate redis, AdmissionMetrics metrics) {
        this.admission = properties.getAdmission();
        this.redis = redis;
        this.metrics = metrics;
    }

    @Override
    public AdmissionLease admitIngress(GatewayRequestContext context) {
        validateIdentity(context);
        return admit("ingress", List.of(
                Limit.rate("tenant", context.tenantId(), admission.getTenantRequestsPerMinute(), 1),
                Limit.rate("user", userKey(context), admission.getUserRequestsPerMinute(), 1),
                Limit.concurrent("tenant", context.tenantId(), admission.getTenantMaxConcurrency()),
                Limit.concurrent("user", userKey(context), admission.getUserMaxConcurrency())
        ));
    }

    @Override
    public AdmissionLease admitUpstream(GatewayRequestContext context, ModelEndpoint endpoint, long estimatedTokens) {
        return admit("upstream", List.of(
                Limit.rate("route", endpoint.key(), admission.getRouteRequestsPerMinute(), 1),
                Limit.rate("provider", endpoint.providerName(), admission.getProviderRequestsPerMinute(), 1),
                Limit.rate("tenant-tpm", context.tenantId(), admission.getTenantTokensPerMinute(), estimatedTokens),
                Limit.rate("user-tpm", userKey(context), admission.getUserTokensPerMinute(), estimatedTokens),
                Limit.rate("route-tpm", endpoint.key(), admission.getRouteTokensPerMinute(), estimatedTokens),
                Limit.rate("provider-tpm", endpoint.providerName(), admission.getProviderTokensPerMinute(), estimatedTokens),
                Limit.concurrent("route", endpoint.key(), admission.getRouteMaxConcurrency()),
                Limit.concurrent("provider", endpoint.providerName(), admission.getProviderMaxConcurrency())
        ));
    }

    @Override
    public void validateRequest(JsonNode request) {
        if (request == null) throw new GatewayException(HttpStatus.BAD_REQUEST, "Request body is required");
        if (request.toString().getBytes(StandardCharsets.UTF_8).length > admission.getMaxRequestBytes()) {
            throw new GatewayException(HttpStatus.PAYLOAD_TOO_LARGE, "Request body exceeds configured limit");
        }
        JsonNode messages = request.path("messages");
        if (messages.isArray() && messages.size() > admission.getMaxMessages()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Message count exceeds configured limit");
        }
        long output = request.path("max_tokens").asLong(request.path("max_completion_tokens").asLong(0));
        if (output > admission.getMaxCompletionTokens()) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Requested completion tokens exceed configured limit");
        }
    }

    @Override
    public void validateTokenBounds(long promptTokens, long completionTokens) {
        if (promptTokens > admission.getMaxPromptTokens()) throw new GatewayException(HttpStatus.BAD_REQUEST, "Prompt tokens exceed configured limit");
        if (completionTokens > admission.getMaxCompletionTokens()) throw new GatewayException(HttpStatus.BAD_REQUEST, "Completion tokens exceed configured limit");
    }

    @Override
    public int maxUpstreamAttempts() { return admission.getMaxUpstreamAttempts(); }

    /** 只把启用的维度送入脚本；返回的租约仅持有实际取得的并发 Key。 */
    private AdmissionLease admit(String stage, List<Limit> candidates) {
        List<Limit> limits = candidates.stream().filter(Limit::enabled).toList();
        if (limits.isEmpty()) return AdmissionLease.NOOP;
        List<String> keys = limits.stream().map(Limit::redisKey).toList();
        List<String> args = new ArrayList<>(4 + limits.size() * 3);
        args.add(String.valueOf(limits.size()));
        args.add(String.valueOf(System.currentTimeMillis()));
        args.add(String.valueOf(admission.getRateBurstSeconds()));
        args.add(String.valueOf(admission.getConcurrencyLeaseTtlSeconds()));
        limits.forEach(limit -> { args.add(limit.concurrent ? "CONCURRENT" : "RATE"); args.add(String.valueOf(limit.limit)); args.add(String.valueOf(limit.amount)); });
        List result = redis.execute(ADMIT_SCRIPT, keys, args.toArray(String[]::new));
        if (result == null || result.isEmpty()) {
            recordUnavailable(stage);
            throw new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "Admission state store unavailable");
        }
        if (!"OK".equals(String.valueOf(result.get(0)))) {
            int index = Integer.parseInt(String.valueOf(result.get(1))) - 1;
            long retry = Long.parseLong(String.valueOf(result.get(3)));
            Limit limit = limits.get(index);
            long observed = result.size() > 4 ? Long.parseLong(String.valueOf(result.get(4))) : limit.limit;
            long requested = result.size() > 5 ? Long.parseLong(String.valueOf(result.get(5))) : limit.amount;
            recordRejected(stage, limit.scope, limit.reasonCode());
            throw new AdmissionRejectedException(limit.scope, retry, limit.reasonCode(),
                    limit.limit, observed, requested);
        }
        recordAllowed(stage);
        if (result.size() > 2) {
            int index = Integer.parseInt(String.valueOf(result.get(1))) - 1;
            if (index >= 0 && index < limits.size()) {
                Limit limit = limits.get(index);
                recordUtilization(stage, limit, Double.parseDouble(String.valueOf(result.get(2))));
            }
        }
        List<String> concurrencyKeys = limits.stream().filter(Limit::concurrent).map(Limit::redisKey).toList();
        AtomicBoolean released = new AtomicBoolean();
        return () -> { if (released.compareAndSet(false, true) && !concurrencyKeys.isEmpty()) redis.execute(RELEASE_SCRIPT, concurrencyKeys); };
    }


    private void validateIdentity(GatewayRequestContext context) {
        if (context.tenantId().isBlank() || context.userId().isBlank()) throw new GatewayException(HttpStatus.UNAUTHORIZED, "Trusted caller identity is required");
    }

    private String userKey(GatewayRequestContext context) { return context.tenantId() + ":" + context.userId(); }

    private void recordAllowed(String stage) { if (metrics != null) metrics.allowed(stage); }
    private void recordRejected(String stage, String scope, String reason) { if (metrics != null) metrics.rejected(stage, scope, reason); }
    private void recordUnavailable(String stage) { if (metrics != null) metrics.unavailable(stage); }
    private void recordUtilization(String stage, Limit limit, double utilization) {
        if (metrics != null) metrics.utilization(stage, limit.metricScope(), limit.dimension(), utilization);
    }

    /** Redis Key 只由服务端受信身份和已配置路由构成，仍做规范化以隔离不安全字符。 */
    private record Limit(String scope, String id, long limit, long amount, boolean concurrent) {
        static Limit rate(String scope, String id, long limit, long amount) { return new Limit(scope, id, limit, Math.max(0, amount), false); }
        static Limit concurrent(String scope, String id, long limit) { return new Limit(scope, id, limit, 1, true); }
        boolean enabled() { return limit > 0 && (!concurrent || amount > 0); }
        String redisKey() { return "llm-gateway:admission:" + (concurrent ? "inflight:" : "bucket:") + scope + ":" + id.replaceAll("[^a-zA-Z0-9._:-]", "_"); }
        String reasonCode() {
            String normalizedScope = scope.endsWith("-tpm") ? scope.substring(0, scope.length() - 4) : scope;
            String dimension = concurrent ? "CONCURRENCY" : scope.endsWith("-tpm") ? "TPM" : "RPM";
            return "ADMISSION_" + normalizedScope.toUpperCase() + "_" + dimension;
        }
        String metricScope() { return scope.endsWith("-tpm") ? scope.substring(0, scope.length() - 4) : scope; }
        String dimension() { return concurrent ? "concurrency" : scope.endsWith("-tpm") ? "tpm" : "rpm"; }
    }
}
