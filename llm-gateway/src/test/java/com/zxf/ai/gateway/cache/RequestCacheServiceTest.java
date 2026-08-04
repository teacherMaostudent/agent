package com.zxf.ai.gateway.cache;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

class RequestCacheServiceTest {
    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void identicalConcurrentMissesShareOneUpstreamSubscription() {
        GatewayProperties properties = new GatewayProperties();
        properties.getCache().setEnabled(true);
        properties.getCache().setRequireExplicitOptIn(true);
        RequestCacheService service = new RequestCacheService(properties, (RuntimeStateRepository) null);
        JsonNode request = mapper.createObjectNode()
                .put("temperature", 0)
                .set("gateway", mapper.createObjectNode().put("cacheable", true));
        AtomicInteger subscriptions = new AtomicInteger();

        Mono<JsonNode> first = service.cachedOrCompute("tenant-a", request, () ->
                Mono.defer(() -> {
                    subscriptions.incrementAndGet();
                    return Mono.delay(Duration.ofMillis(10))
                            .thenReturn(mapper.createObjectNode().put("answer", "ok"));
                }));
        Mono<JsonNode> second = service.cachedOrCompute("tenant-a", request, () ->
                Mono.defer(() -> {
                    subscriptions.incrementAndGet();
                    return Mono.just(mapper.createObjectNode().put("answer", "unexpected"));
                }));

        StepVerifier.create(Mono.zip(first, second))
                .assertNext(pair -> {
                    org.assertj.core.api.Assertions.assertThat(pair.getT1()).isEqualTo(pair.getT2());
                })
                .verifyComplete();
        org.assertj.core.api.Assertions.assertThat(subscriptions).hasValue(1);
    }

    @Test
    void cacheRequiresExplicitOptInAndRejectsToolRequests() {
        GatewayProperties properties = new GatewayProperties();
        properties.getCache().setEnabled(true);
        RequestCacheService service = new RequestCacheService(properties, (RuntimeStateRepository) null);
        AtomicInteger loads = new AtomicInteger();
        JsonNode request = mapper.createObjectNode().put("temperature", 0);

        service.cachedOrCompute("tenant-a", request, () ->
                Mono.just(mapper.createObjectNode().put("load", loads.incrementAndGet()))).block();
        service.cachedOrCompute("tenant-a", request, () ->
                Mono.just(mapper.createObjectNode().put("load", loads.incrementAndGet()))).block();

        org.assertj.core.api.Assertions.assertThat(loads).hasValue(2);
    }
}
