package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import com.zxf.ai.gateway.rag.RagAgentClient;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

/** Authenticated gateway entry point for the bounded RAG Agent Graph. */
@RestController
@RequestMapping("/v1/agent")
@ConditionalOnProperty(prefix = "rag-agent", name = "enabled", havingValue = "true")
public class AgentController {
    private final RagAgentClient ragAgentClient;
    private final ApiKeyService apiKeyService;
    private final GatewayProperties properties;
    private final ApplicationEventPublisher eventPublisher;
    private final ObjectMapper objectMapper;

    public AgentController(RagAgentClient ragAgentClient, ApiKeyService apiKeyService,
                           GatewayProperties properties, ApplicationEventPublisher eventPublisher,
                           ObjectMapper objectMapper) {
        this.ragAgentClient = ragAgentClient;
        this.apiKeyService = apiKeyService;
        this.properties = properties;
        this.eventPublisher = eventPublisher;
        this.objectMapper = objectMapper;
    }

    @PostMapping("/run")
    public Mono<JsonNode> run(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestHeader(value = "X-Request-Id", required = false) String requestId,
            @RequestHeader(value = "X-Api-Key", required = false) String xApiKey,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody JsonNode request
    ) {
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(authorization, xApiKey, userId,
                properties.getDefaultModel());
        String resolvedRequestId = requestId == null || requestId.isBlank()
                ? "agent-" + UUID.randomUUID() : requestId;
        Instant started = Instant.now();
        return ragAgentClient.runAgent(request, auth.tenantId(), auth.userId(), resolvedRequestId)
                .doOnSuccess(response -> publishTrace(resolvedRequestId, auth, request, response, started, null))
                .doOnError(error -> publishTrace(resolvedRequestId, auth, request,
                        objectMapper.createObjectNode(), started, error));
    }

    private void publishTrace(String requestId, ApiKeyService.AuthResult auth, JsonNode request,
                              JsonNode response, Instant started, Throwable error) {
        eventPublisher.publishEvent(new GatewayTraceEvent(
                requestId, auth.tenantId(), auth.userId(), properties.getDefaultModel(), false,
                started, Instant.now(), Duration.between(started, Instant.now()).toMillis(),
                request.deepCopy(), response.deepCopy(), error == null,
                error == null ? "" : error.getClass().getSimpleName(),
                error == null || error.getMessage() == null ? "" : error.getMessage(),
                BigDecimal.ZERO, "CNY"));
    }
}
