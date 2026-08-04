package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.BodyInserters;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import reactor.core.publisher.Mono;

@Component
@ConditionalOnProperty(prefix = "rag-agent", name = "enabled", havingValue = "true")
public class RagAgentClient {
    private final WebClient client;
    private final RagAgentProperties properties;

    public RagAgentClient(WebClient.Builder webClientBuilder, RagAgentProperties properties) {
        this.properties = properties;
        this.client = webClientBuilder
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("X-Rag-Agent-Key", properties.getApiKey())
                .build();
    }

    public Mono<JsonNode> uploadDocument(FilePart file, String businessId, String documentType) {
        ensureEnabled();
        MultipartBodyBuilder builder = new MultipartBodyBuilder();
        builder.asyncPart("file", file.content(), org.springframework.core.io.buffer.DataBuffer.class)
                .filename(file.filename());
        addTextPart(builder, "business_id", businessId);
        addTextPart(builder, "document_type", documentType);
        return client.post()
                .uri("/api/v1/documents/upload")
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(BodyInserters.fromMultipartData(builder.build()))
                .retrieve()
                .bodyToMono(JsonNode.class)
                .timeout(properties.getResponseTimeout())
                .onErrorMap(this::toGatewayException);
    }

    public Mono<JsonNode> runAgent(JsonNode request, String tenantId, String userId, String requestId) {
        ensureEnabled();
        return client.post()
                .uri("/api/v1/agent/run")
                .contentType(MediaType.APPLICATION_JSON)
                .header("X-Tenant-Id", tenantId)
                .header("X-User-Id", userId)
                .header("X-Request-Id", requestId)
                .header("X-Permissions", properties.getPermissions())
                .bodyValue(request)
                .retrieve()
                .bodyToMono(JsonNode.class)
                .timeout(properties.getResponseTimeout())
                .onErrorMap(this::toGatewayException);
    }

    private void addTextPart(MultipartBodyBuilder builder, String name, String value) {
        if (value != null && !value.isBlank()) {
            builder.part(name, value);
        }
    }

    private void ensureEnabled() {
        if (!properties.isEnabled()) {
            throw new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "rag-agent-service integration is disabled.");
        }
    }

    private Throwable toGatewayException(Throwable throwable) {
        if (throwable instanceof GatewayException) {
            return throwable;
        }
        if (throwable instanceof WebClientResponseException ex) {
            String body = ex.getResponseBodyAsString();
            String message = body == null || body.isBlank() ? ex.getMessage() : body;
            return new GatewayException(HttpStatus.BAD_GATEWAY, "rag-agent-service failed: " + message);
        }
        return new GatewayException(HttpStatus.BAD_GATEWAY, "rag-agent-service unavailable: " + throwable.getMessage());
    }
}
