package com.zxf.ai.gateway.cache;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;

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
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.Supplier;

@Component
public class RequestCacheService {
    private final GatewayProperties properties;
    private final RuntimeStateRepository stateRepository;
    private final Map<String, CacheEntry> entries = new LinkedHashMap<>(128, 0.75f, true);
    private final Map<String, ReentrantLock> localMutexes = new ConcurrentHashMap<>();
    private long hits;
    private long misses;
    private long mutexWaits;

    public RequestCacheService(GatewayProperties properties, ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.properties = properties;
        this.stateRepository = stateRepository.getIfAvailable();
    }

    public Mono<JsonNode> cachedOrCompute(String tenantId, JsonNode request, Supplier<Mono<JsonNode>> loader) {
        if (!cacheable(request)) {
            return loader.get();
        }
        Optional<JsonNode> cached = get(tenantId, request);
        if (cached.isPresent()) {
            return Mono.just(cached.get());
        }
        if (!properties.getCache().isMutexProtectionEnabled()) {
            return loader.get().doOnNext(response -> put(tenantId, request, response));
        }
        String key = key(tenantId, request);
        ReentrantLock lock = localMutexes.computeIfAbsent(key, ignored -> new ReentrantLock());
        mutexWaits++;
        lock.lock();
        try {
            Optional<JsonNode> cachedAfterLock = get(tenantId, request);
            if (cachedAfterLock.isPresent()) {
                return Mono.just(cachedAfterLock.get());
            }
            return loader.get().doOnNext(response -> put(tenantId, request, response));
        } finally {
            lock.unlock();
            localMutexes.remove(key, lock);
        }
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
        snapshot.put("mutexWaits", mutexWaits);
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
        return properties.getCache().isEnabled() && !request.path("stream").asBoolean(false);
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
        String raw = tenantId + ":" + request.toString();
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
