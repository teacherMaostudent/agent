package com.zxf.ai.gateway.cache;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

@Component
public class RequestCacheService {
    private final GatewayProperties properties;
    private final RuntimeStateRepository stateRepository;
    private final Map<String, CacheEntry> entries = new LinkedHashMap<>(128, 0.75f, true);
    private final Map<String, Mono<JsonNode>> inFlight = new ConcurrentHashMap<>();
    private long hits;
    private long misses;
    private final AtomicLong mutexWaits = new AtomicLong();

    @Autowired
    public RequestCacheService(GatewayProperties properties, ObjectProvider<RuntimeStateRepository> stateRepository) {
        this(properties, stateRepository.getIfAvailable());
    }

    RequestCacheService(GatewayProperties properties, RuntimeStateRepository stateRepository) {
        this.properties = properties;
        this.stateRepository = stateRepository;
    }

    public Mono<JsonNode> cachedOrCompute(String tenantId, JsonNode request, Supplier<Mono<JsonNode>> loader) {
        if (!cacheable(request)) {
            return loader.get();
        }
        return Mono.defer(() -> cachedOrComputeCacheable(tenantId, request, loader))
                .subscribeOn(Schedulers.boundedElastic());
    }

    private Mono<JsonNode> cachedOrComputeCacheable(
            String tenantId, JsonNode request, Supplier<Mono<JsonNode>> loader) {
        Optional<JsonNode> cached = get(tenantId, request);
        if (cached.isPresent()) {
            return Mono.just(cached.get());
        }
        if (!properties.getCache().isMutexProtectionEnabled()) {
            return loader.get().doOnNext(response -> put(tenantId, request, response));
        }
        String key = key(tenantId, request);
        Mono<JsonNode> existing = inFlight.get(key);
        if (existing != null) {
            mutexWaits.incrementAndGet();
            return existing;
        }
        Mono<JsonNode> created = Mono.defer(() -> {
                    Optional<JsonNode> cachedAfterJoin = get(tenantId, request);
                    if (cachedAfterJoin.isPresent()) {
                        return Mono.just(cachedAfterJoin.get());
                    }
                    return loader.get().doOnNext(response -> put(tenantId, request, response));
                })
                .cache();
        Mono<JsonNode> winner = inFlight.putIfAbsent(key, created);
        Mono<JsonNode> selected = winner == null ? created : winner;
        if (winner != null) {
            mutexWaits.incrementAndGet();
        }
        return selected.doFinally(ignored -> inFlight.remove(key, selected));
    }

    public Optional<JsonNode> get(String tenantId, JsonNode request) {
        if (!cacheable(request)) {
            return Optional.empty();
        }
        String key = key(tenantId, request);
        if (stateRepository != null) {
            Optional<JsonNode> cached = stateRepository.getCache(key);
            stateRepository.incrementStat(cached.isPresent() ? "cache_hits" : "cache_misses");
            return cached;
        }
        synchronized (entries) {
            CacheEntry entry = entries.get(key);
            if (entry == null || entry.expiresAt().isBefore(Instant.now())) {
                entries.remove(key);
                misses++;
                return Optional.empty();
            }
            hits++;
            return Optional.of(entry.response().deepCopy());
        }
    }

    public void put(String tenantId, JsonNode request, JsonNode response) {
        if (!cacheable(request)) {
            return;
        }
        Instant expiresAt = Instant.now().plus(ttlWithJitter());
        if (stateRepository != null) {
            stateRepository.putCache(key(tenantId, request), tenantId, response.deepCopy(), expiresAt);
            return;
        }
        synchronized (entries) {
            while (entries.size() >= properties.getCache().getMaxEntries()) {
                String firstKey = entries.keySet().iterator().next();
                entries.remove(firstKey);
            }
            entries.put(key(tenantId, request), new CacheEntry(response.deepCopy(), expiresAt));
        }
    }

    public Map<String, Object> snapshot() {
        Map<String, Object> snapshot = new LinkedHashMap<>();
        if (stateRepository != null) {
            snapshot.putAll(stateRepository.cacheSnapshot());
            snapshot.put("store", "mysql");
        } else {
            synchronized (entries) {
                snapshot.put("store", "memory");
                snapshot.put("entries", entries.size());
                snapshot.put("hits", hits);
                snapshot.put("misses", misses);
            }
        }
        snapshot.put("enabled", properties.getCache().isEnabled());
        snapshot.put("ttl", properties.getCache().getTtl().toString());
        snapshot.put("randomTtlJitter", properties.getCache().getRandomTtlJitter().toString());
        snapshot.put("mutexProtectionEnabled", properties.getCache().isMutexProtectionEnabled());
        snapshot.put("mutexWaits", mutexWaits.get());
        snapshot.put("interviewNotes", Map.of(
                "cachePenetration", "Cache only accepts valid gateway requests; invalid model routes are rejected before upstream calls.",
                "cacheBreakdown", "A local mutex prevents many identical misses from calling the upstream model at the same time.",
                "cacheAvalanche", "Random TTL jitter spreads cache expiration time and reduces synchronized expiry."
        ));
        return snapshot;
    }

    public void clear() {
        if (stateRepository != null) {
            stateRepository.clearCache();
            return;
        }
        synchronized (entries) {
            entries.clear();
        }
    }

    private boolean cacheable(JsonNode request) {
        if (!properties.getCache().isEnabled() || request.path("stream").asBoolean(false)) {
            return false;
        }
        if (properties.getCache().isRequireExplicitOptIn()
                && !request.path("gateway").path("cacheable").asBoolean(false)) {
            return false;
        }
        return !request.has("tools") && request.path("temperature").asDouble(0.0d) == 0.0d;
    }

    private Duration ttlWithJitter() {
        Duration ttl = properties.getCache().getTtl();
        Duration jitter = properties.getCache().getRandomTtlJitter();
        if (jitter == null || jitter.isZero() || jitter.isNegative()) {
            return ttl;
        }
        long jitterMillis = ThreadLocalRandom.current().nextLong(jitter.toMillis() + 1);
        return ttl.plusMillis(jitterMillis);
    }

    private String key(String tenantId, JsonNode request) {
        String policyVersion = properties.getRoutes().toString() + ":" + properties.getPromptTemplates().toString();
        String raw = tenantId + ":" + policyVersion + ":" + request.toString();
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(raw.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception ex) {
            return Integer.toHexString(raw.hashCode());
        }
    }

    private record CacheEntry(JsonNode response, Instant expiresAt) {
    }
}
