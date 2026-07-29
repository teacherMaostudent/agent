package com.zxf.ai.gateway.client;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.util.retry.Retry;

import java.time.Duration;

@Component
public class OpenAiCompatibleClient implements LlmProviderClient {
    private final WebClient.Builder webClientBuilder;
    private final ObjectMapper objectMapper;
    private final GatewayProperties properties;

    public OpenAiCompatibleClient(WebClient.Builder webClientBuilder, ObjectMapper objectMapper, GatewayProperties properties) {
        this.webClientBuilder = webClientBuilder;
        this.objectMapper = objectMapper;
        this.properties = properties;
    }

    @Override
    public String protocol() {
        return "openai-compatible";
    }

    /**
     * 调用 OpenAI Compatible 的非流式 Chat Completions 接口。
     *
     * <p>DeepSeek、OpenAI 以及很多本地模型服务都兼容这套协议，所以这里不按厂商拆多个客户端，
     * 而是通过 ModelEndpoint 中的 baseUrl、apiKey、upstreamModel 来动态适配。</p>
     */
    @Override
    public Mono<JsonNode> chatCompletion(ModelEndpoint endpoint, JsonNode originalRequest) {
        JsonNode upstreamRequest = withUpstreamModel(endpoint, originalRequest, false);
        return client(endpoint)
                .post()
                .uri("/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.APPLICATION_JSON)
                .bodyValue(upstreamRequest)
                .retrieve()
                .bodyToMono(JsonNode.class)
                // 超时必须放在上游调用链路上，避免模型长时间无响应时连接一直占用。
                .timeout(properties.getRequestTimeout())
                .transform(this::applyRetryIfEnabled)
                .onErrorMap(this::mapError);
    }

    /**
     * 调用 OpenAI Compatible 的流式 Chat Completions 接口。
     *
     * <p>流式模式会把 stream 强制设置为 true，并用 text/event-stream 接收上游分片。
     * Controller 层再把这些分片透传给调用方。</p>
     */
    @Override
    public Flux<String> streamChatCompletion(ModelEndpoint endpoint, JsonNode originalRequest) {
        JsonNode upstreamRequest = withUpstreamModel(endpoint, originalRequest, true);
        return client(endpoint)
                .post()
                .uri("/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .bodyValue(upstreamRequest)
                .retrieve()
                .bodyToFlux(String.class)
                .timeout(properties.getRequestTimeout())
                .transform(this::applyRetryIfEnabled)
                .onErrorMap(this::mapError);
    }

    private WebClient client(ModelEndpoint endpoint) {
        // WebClient.Builder 是 Spring 管理的共享 builder，clone 后再设置 baseUrl/header，
        // 可以避免不同 provider 的 baseUrl 和 Authorization 互相污染。
        WebClient.Builder builder = webClientBuilder.clone()
                .baseUrl(trimTrailingSlash(endpoint.provider().getBaseUrl()));
        String apiKey = endpoint.provider().getApiKey();
        if (apiKey != null && !apiKey.isBlank()) {
            // OpenAI Compatible API 通常使用 Bearer Token 鉴权。
            builder.defaultHeader(HttpHeaders.AUTHORIZATION, "Bearer " + apiKey);
        }
        return builder.build();
    }

    private JsonNode withUpstreamModel(ModelEndpoint endpoint, JsonNode originalRequest, boolean stream) {
        // 对外暴露的 model 可以是网关逻辑模型名；真正发给厂商的模型名使用 upstreamModel。
        // deepCopy 可以避免修改 Controller 收到的原始 JsonNode，降低副作用。
        ObjectNode copy = originalRequest.deepCopy();
        copy.put("model", endpoint.upstreamModel());
        copy.put("stream", stream);
        if (stream && !copy.has("stream_options")
                && !"ollama".equalsIgnoreCase(endpoint.providerName())
                && !"vllm".equalsIgnoreCase(endpoint.providerName())) {
            // 主流 OpenAI-compatible 厂商可在最后一个 SSE chunk 返回 usage。
            // 本地推理服务版本差异较大，默认不注入以保持兼容。
            copy.putObject("stream_options").put("include_usage", true);
        }
        return copy;
    }

    private Retry retrySpec() {
        // 指数退避比固定间隔更温和，能减少上游短暂故障时的瞬时流量放大。
        return Retry.backoff(properties.getMaxRetries(), Duration.ofMillis(300))
                .filter(this::isRetryable);
    }

    private <T> Mono<T> applyRetryIfEnabled(Mono<T> mono) {
        // Reactor 的 retryWhen 在 0 次重试时会直接抛 Retries exhausted，单独跳过更符合直觉。
        if (properties.getMaxRetries() <= 0) {
            return mono;
        }
        return mono.retryWhen(retrySpec());
    }

    private <T> Flux<T> applyRetryIfEnabled(Flux<T> flux) {
        if (properties.getMaxRetries() <= 0) {
            return flux;
        }
        return flux.retryWhen(retrySpec());
    }

    private boolean isRetryable(Throwable throwable) {
        if (throwable instanceof WebClientResponseException responseException) {
            // 5xx 和 429 通常可能是临时性问题，适合重试；4xx 鉴权、参数、模型不存在通常不重试。
            return responseException.getStatusCode().is5xxServerError()
                    || responseException.getStatusCode().value() == 429;
        }
        // 网络超时、连接异常这类非 HTTP 错误一般也可以尝试重试。
        return true;
    }

    private Throwable mapError(Throwable throwable) {
        if (throwable instanceof GatewayException) {
            return throwable;
        }
        if (throwable instanceof WebClientResponseException responseException) {
            // 保留上游响应体，方便看到 billing_not_active、invalid_api_key、model_not_found 等真实原因。
            String body = responseException.getResponseBodyAsString();
            String message = body == null || body.isBlank() ? responseException.getMessage() : body;
            return new GatewayException(HttpStatus.valueOf(responseException.getStatusCode().value()), message);
        }
        // 非 HTTP 异常通常来自网络、DNS、代理或超时，统一包装为 502。
        return new GatewayException(HttpStatus.BAD_GATEWAY, throwable.getMessage());
    }

    private String trimTrailingSlash(String baseUrl) {
        if (baseUrl == null) {
            return "";
        }
        return baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
    }
}
