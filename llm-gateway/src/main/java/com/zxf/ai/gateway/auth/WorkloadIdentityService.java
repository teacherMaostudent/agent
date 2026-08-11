package com.zxf.ai.gateway.auth;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.reactive.function.BodyInserters;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.time.Instant;

/** Supplies short-lived OAuth2 client-credentials tokens to outbound platform calls. */
@Component
public class WorkloadIdentityService {
    private final WebClient client;
    private final String tokenUrl;
    private final String clientId;
    private final String clientSecret;
    private final String audience;
    private final String scope;
    private volatile String accessToken = "";
    private volatile Instant expiresAt = Instant.EPOCH;

    /**
     * 初始化 workload identity service 所需的依赖与运行期状态。
    */
    public WorkloadIdentityService(
            WebClient.Builder builder,
            @Value("${workload-identity.token-url:}") String tokenUrl,
            @Value("${workload-identity.client-id:llm-gateway}") String clientId,
            @Value("${workload-identity.client-secret:}") String clientSecret,
            @Value("${workload-identity.audience:agent-platform}") String audience,
            @Value("${workload-identity.scope:}") String scope
    ) {
        this.client = builder.clone().build();
        this.tokenUrl = tokenUrl;
        this.clientId = clientId;
        this.clientSecret = clientSecret;
        this.audience = audience;
        this.scope = scope;
    }

    /**
     * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
    */
    public boolean isEnabled() {
        return !tokenUrl.isBlank() && !clientId.isBlank() && !clientSecret.isBlank();
    }

    /**
     * 执行 authorization header 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<String> authorizationHeader() {
        if (!isEnabled()) {
            return Mono.just("");
        }
        String current = accessToken;
        if (!current.isBlank() && Instant.now().isBefore(expiresAt)) {
            return Mono.just("Bearer " + current);
        }
        MultiValueMap<String, String> form = new LinkedMultiValueMap<>();
        form.add("grant_type", "client_credentials");
        if (!audience.isBlank()) form.add("audience", audience);
        if (!scope.isBlank()) form.add("scope", scope);
        return client.post()
                .uri(tokenUrl)
                .headers(headers -> headers.setBasicAuth(clientId, clientSecret))
                .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                .body(BodyInserters.fromFormData(form))
                .retrieve()
                .bodyToMono(JsonNode.class)
                .map(body -> {
                    String token = body.path("access_token").asText("");
                    if (token.isBlank()) {
                        throw new IllegalStateException("OIDC token endpoint omitted access_token");
                    }
                    long expiresIn = Math.max(30, body.path("expires_in").asLong(300));
                    accessToken = token;
                    expiresAt = Instant.now().plusSeconds(Math.max(1, expiresIn - 30));
                    return "Bearer " + token;
                });
    }
}
