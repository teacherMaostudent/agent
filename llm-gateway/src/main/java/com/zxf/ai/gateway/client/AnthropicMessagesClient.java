package com.zxf.ai.gateway.client;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.util.retry.Retry;

import java.time.Duration;
import java.time.Instant;

/**
 * Native Claude / Anthropic Messages API client.
 *
 * <p>Claude does not use the OpenAI Chat Completions protocol. This adapter accepts the
 * gateway's unified OpenAI-style request, calls Anthropic Messages API upstream, and converts
 * the response back to an OpenAI-compatible shape so business callers do not need provider-specific code.</p>
 */
@Component
public class AnthropicMessagesClient implements LlmProviderClient {
    private static final String ANTHROPIC_VERSION = "2023-06-01";

    private final WebClient.Builder webClientBuilder;
    private final ObjectMapper objectMapper;
    private final GatewayProperties properties;

    /**
     * 初始化 anthropic messages client 所需的依赖与运行期状态。
    */
    public AnthropicMessagesClient(WebClient.Builder webClientBuilder, ObjectMapper objectMapper, GatewayProperties properties) {
        this.webClientBuilder = webClientBuilder;
        this.objectMapper = objectMapper;
        this.properties = properties;
    }

    @Override
    /**
     * 执行 protocol 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public String protocol() {
        return "anthropic";
    }

    @Override
    /**
     * 执行 chat completion 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> chatCompletion(ModelEndpoint endpoint, JsonNode originalRequest) {
        ObjectNode upstreamRequest = toAnthropicRequest(endpoint, originalRequest, false);
        return client(endpoint)
                .post()
                .uri("/v1/messages")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.APPLICATION_JSON)
                .bodyValue(upstreamRequest)
                .retrieve()
                .bodyToMono(JsonNode.class)
                .timeout(properties.getRequestTimeout())
                .transform(this::applyRetryIfEnabled)
                .map(response -> toOpenAiCompatibleResponse(endpoint, response))
                .onErrorMap(this::mapError);
    }

    @Override
    /**
     * 执行 stream chat completion 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Flux<String> streamChatCompletion(ModelEndpoint endpoint, JsonNode originalRequest) {
        ObjectNode upstreamRequest = toAnthropicRequest(endpoint, originalRequest, true);
        return client(endpoint)
                .post()
                .uri("/v1/messages")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .bodyValue(upstreamRequest)
                .retrieve()
                .bodyToFlux(String.class)
                .timeout(properties.getRequestTimeout())
                .transform(this::applyRetryIfEnabled)
                .onErrorMap(this::mapError);
    }

    /**
     * 执行 client 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private WebClient client(ModelEndpoint endpoint) {
        WebClient.Builder builder = webClientBuilder.clone()
                .baseUrl(trimTrailingSlash(endpoint.provider().getBaseUrl()))
                .defaultHeader("anthropic-version", ANTHROPIC_VERSION);
        String apiKey = endpoint.provider().getApiKey();
        if (apiKey != null && !apiKey.isBlank()) {
            builder.defaultHeader("x-api-key", apiKey);
        }
        return builder.build();
    }

    /**
     * 执行 to anthropic request 的协议或数据转换，保持内部模型与外部契约隔离。
    */
    private ObjectNode toAnthropicRequest(ModelEndpoint endpoint, JsonNode originalRequest, boolean stream) {
        ObjectNode request = objectMapper.createObjectNode();
        request.put("model", endpoint.upstreamModel());
        request.put("max_tokens", maxTokens(originalRequest));
        request.put("stream", stream);

        if (originalRequest.has("temperature")) {
            request.set("temperature", originalRequest.path("temperature"));
        }
        if (originalRequest.has("top_p")) {
            request.set("top_p", originalRequest.path("top_p"));
        }

        String system = systemPrompt(originalRequest);
        if (!system.isBlank()) {
            request.put("system", system);
        }

        ArrayNode messages = request.putArray("messages");
        JsonNode originalMessages = originalRequest.path("messages");
        if (originalMessages.isArray()) {
            for (JsonNode message : originalMessages) {
                String role = message.path("role").asText("user");
                if ("system".equals(role)) {
                    continue;
                }
                ObjectNode converted = messages.addObject();
                converted.put("role", "assistant".equals(role) ? "assistant" : "user");
                JsonNode content = message.path("content");
                if (content.isMissingNode() || content.isNull()) {
                    converted.put("content", "");
                } else {
                    converted.set("content", content.deepCopy());
                }
            }
        }
        if (messages.isEmpty()) {
            messages.addObject()
                    .put("role", "user")
                    .put("content", originalRequest.toString());
        }
        return request;
    }

    /**
     * 执行 max tokens 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private int maxTokens(JsonNode originalRequest) {
        if (originalRequest.has("max_tokens")) {
            return originalRequest.path("max_tokens").asInt(1024);
        }
        if (originalRequest.has("max_completion_tokens")) {
            return originalRequest.path("max_completion_tokens").asInt(1024);
        }
        return 1024;
    }

    /**
     * 执行 system prompt 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private String systemPrompt(JsonNode originalRequest) {
        StringBuilder builder = new StringBuilder();
        JsonNode messages = originalRequest.path("messages");
        if (messages.isArray()) {
            for (JsonNode message : messages) {
                if ("system".equals(message.path("role").asText())) {
                    if (!builder.isEmpty()) {
                        builder.append("\n\n");
                    }
                    JsonNode content = message.path("content");
                    builder.append(content.isTextual() ? content.asText() : content.toString());
                }
            }
        }
        return builder.toString();
    }

    /**
     * 执行 to open ai compatible response 的协议或数据转换，保持内部模型与外部契约隔离。
    */
    private JsonNode toOpenAiCompatibleResponse(ModelEndpoint endpoint, JsonNode anthropicResponse) {
        ObjectNode response = objectMapper.createObjectNode();
        response.put("id", anthropicResponse.path("id").asText(""));
        response.put("object", "chat.completion");
        response.put("created", Instant.now().getEpochSecond());
        response.put("model", endpoint.modelName());

        ArrayNode choices = response.putArray("choices");
        ObjectNode choice = choices.addObject();
        choice.put("index", 0);
        ObjectNode message = choice.putObject("message");
        message.put("role", "assistant");
        message.put("content", textContent(anthropicResponse.path("content")));
        choice.putNull("logprobs");
        choice.put("finish_reason", anthropicResponse.path("stop_reason").asText("stop"));

        JsonNode usage = anthropicResponse.path("usage");
        long baseInputTokens = usage.path("input_tokens").asLong(0);
        long cacheReadTokens = usage.path("cache_read_input_tokens").asLong(0);
        long cacheCreationTokens = usage.path("cache_creation_input_tokens").asLong(0);
        long promptTokens = baseInputTokens + cacheReadTokens + cacheCreationTokens;
        long completionTokens = usage.path("output_tokens").asLong(0);
        ObjectNode openAiUsage = response.putObject("usage");
        openAiUsage.put("prompt_tokens", promptTokens);
        openAiUsage.put("completion_tokens", completionTokens);
        openAiUsage.put("total_tokens", promptTokens + completionTokens);
        openAiUsage.put("cache_read_input_tokens", cacheReadTokens);
        openAiUsage.put("cache_creation_input_tokens", cacheCreationTokens);

        ObjectNode raw = response.putObject("anthropic");
        raw.set("raw", anthropicResponse);
        return response;
    }

    /**
     * 执行 text content 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private String textContent(JsonNode content) {
        if (!content.isArray()) {
            return content.asText("");
        }
        StringBuilder builder = new StringBuilder();
        for (JsonNode block : content) {
            if ("text".equals(block.path("type").asText())) {
                builder.append(block.path("text").asText(""));
            }
        }
        return builder.toString();
    }

    /**
     * 执行 retry spec 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private Retry retrySpec() {
        return Retry.backoff(properties.getMaxRetries(), Duration.ofMillis(300))
                .filter(this::isRetryable);
    }

    /**
     * 执行 apply retry if enabled 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private <T> Mono<T> applyRetryIfEnabled(Mono<T> mono) {
        if (properties.getMaxRetries() <= 0) {
            return mono;
        }
        return mono.retryWhen(retrySpec());
    }

    /**
     * 执行 apply retry if enabled 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private <T> Flux<T> applyRetryIfEnabled(Flux<T> flux) {
        if (properties.getMaxRetries() <= 0) {
            return flux;
        }
        return flux.retryWhen(retrySpec());
    }

    /**
     * 读取当前配置或运行状态字段 is retryable 的值，供调用方进行受控决策。
    */
    private boolean isRetryable(Throwable throwable) {
        if (throwable instanceof WebClientResponseException responseException) {
            return responseException.getStatusCode().is5xxServerError()
                    || responseException.getStatusCode().value() == 429;
        }
        return true;
    }

    /**
     * 执行 map error 的协议或数据转换，保持内部模型与外部契约隔离。
    */
    private Throwable mapError(Throwable throwable) {
        if (throwable instanceof GatewayException) {
            return throwable;
        }
        if (throwable instanceof WebClientResponseException responseException) {
            String body = responseException.getResponseBodyAsString();
            String message = body == null || body.isBlank() ? responseException.getMessage() : body;
            return new GatewayException(HttpStatus.valueOf(responseException.getStatusCode().value()), message);
        }
        return new GatewayException(HttpStatus.BAD_GATEWAY, throwable.getMessage());
    }

    /**
     * 执行 trim trailing slash 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private String trimTrailingSlash(String baseUrl) {
        if (baseUrl == null) {
            return "";
        }
        return baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
    }
}

