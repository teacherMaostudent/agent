package com.zxf.ai.gateway.client;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.ModelEndpoint;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 上游模型厂商客户端抽象。
 */
public interface LlmProviderClient {
    /**
     * 返回该客户端实现的上游模型协议标识，用于安全选择适配器。
     */
    String protocol();

    /**
     * 向指定上游端点发送非流式对话请求，并保留原始请求语义。
     */
    Mono<JsonNode> chatCompletion(ModelEndpoint endpoint, JsonNode originalRequest);

    /**
     * 向指定上游端点发送流式对话请求，并逐段转发可消费的响应内容。
     */
    Flux<String> streamChatCompletion(ModelEndpoint endpoint, JsonNode originalRequest);
}
