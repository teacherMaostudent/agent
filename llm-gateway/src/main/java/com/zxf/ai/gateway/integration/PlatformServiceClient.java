package com.zxf.ai.gateway.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatusCode;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

@Component
public class PlatformServiceClient {
    private final WebClient governance;
    private final WebClient controlPlane;
    private final ObjectMapper objectMapper;
    private final String defaultTenant;

    public PlatformServiceClient(
            WebClient.Builder builder,
            ObjectMapper objectMapper,
            @Value("${platform-services.governance.base-url:http://localhost:8085}") String governanceUrl,
            @Value("${platform-services.control-plane.base-url:http://localhost:8086}") String controlPlaneUrl,
            @Value("${platform-services.default-tenant:demo-tenant}") String defaultTenant
    ) {
        this.governance = builder.clone().baseUrl(governanceUrl).build();
        this.controlPlane = builder.clone().baseUrl(controlPlaneUrl).build();
        this.objectMapper = objectMapper;
        this.defaultTenant = defaultTenant;
    }

    public Mono<JsonNode> governance(String method, String path, JsonNode body,
                                     String tenantId, String userId) {
        return exchange(governance, method, path, body, tenantId, userId, "governance-auditor");
    }

    public Mono<JsonNode> controlPlane(String method, String path, JsonNode body,
                                      String tenantId, String userId) {
        return exchange(controlPlane, method, path, body, tenantId, userId, "agent-admin");
    }

    private Mono<JsonNode> exchange(WebClient client, String method, String path, JsonNode body,
                                    String tenantId, String userId, String role) {
        WebClient.RequestBodySpec request = client.method(org.springframework.http.HttpMethod.valueOf(method))
                .uri(path)
                .header("X-Tenant-Id", value(tenantId, defaultTenant))
                .header("X-User-Id", value(userId, "llm-gateway"))
                .header("X-Roles", role);
        WebClient.RequestHeadersSpec<?> prepared = body == null ? request : request.bodyValue(body);
        return prepared.exchangeToMono(response -> {
            HttpStatusCode status = response.statusCode();
            return response.bodyToMono(String.class).defaultIfEmpty("{}").flatMap(payload -> {
                JsonNode parsed;
                try {
                    parsed = objectMapper.readTree(payload);
                } catch (Exception ignored) {
                    parsed = objectMapper.createObjectNode().put("message", payload);
                }
                if (status.isError()) {
                    return Mono.error(new PlatformServiceException(status.value(), parsed));
                }
                return Mono.just(parsed);
            });
        });
    }

    private String value(String candidate, String fallback) {
        return candidate == null || candidate.isBlank() ? fallback : candidate;
    }

    public static final class PlatformServiceException extends RuntimeException {
        private final int status;
        private final JsonNode body;

        public PlatformServiceException(int status, JsonNode body) {
            super("Platform service returned HTTP " + status);
            this.status = status;
            this.body = body;
        }

        public int status() {
            return status;
        }

        public JsonNode body() {
            return body;
        }
    }
}
