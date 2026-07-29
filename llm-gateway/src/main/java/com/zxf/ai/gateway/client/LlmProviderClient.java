package com.zxf.ai.gateway.client;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.ModelEndpoint;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 上游模型厂商客户端抽象。
 */
public interface LlmProviderClient {
    String protocol();

    Mono<JsonNode> chatCompletion(ModelEndpoint endpoint, JsonNode originalRequest);

    Flux<String> streamChatCompletion(ModelEndpoint endpoint, JsonNode originalRequest);
}
