package com.zxf.ai.gateway.auth;

import com.zxf.ai.gateway.config.GatewayProperties;
import org.springframework.core.Ordered;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.security.config.web.server.SecurityWebFiltersOrder;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;
import reactor.core.publisher.Mono;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/**
 * Enforces an external policy decision after authentication and before model
 * execution.  An unavailable policy service fails closed because allowing a
 * request would bypass the tenant's authorization boundary.
 */
@Component
public class OpaAuthorizationWebFilter implements WebFilter, Ordered {
    private final GatewayProperties properties;
    private final WebClient client;

    /**
     * 初始化 opa authorization web filter 所需的依赖与运行期状态。
    */
    public OpaAuthorizationWebFilter(GatewayProperties properties, WebClient.Builder builder) {
        this.properties = properties;
        this.client = builder.baseUrl(properties.getOpa().getBaseUrl()).build();
    }

    @Override
    /**
     * 在请求进入后续链路前执行 filter，确保身份和授权边界得到落实。
    */
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        if (!properties.getOpa().isEnabled()
                || !exchange.getRequest().getPath().value().equals("/v1/chat/completions")) {
            return chain.filter(exchange);
        }
        var headers = exchange.getRequest().getHeaders();
        Map<String, Object> input = Map.of(
                "subject", Map.of(
                        "tenant_id", headers.getFirst("X-Tenant-Id") == null ? "" : headers.getFirst("X-Tenant-Id"),
                        "user_id", headers.getFirst("X-User-Id") == null ? "" : headers.getFirst("X-User-Id"),
                        "roles", List.of(headers.getFirst("X-Roles") == null ? "" : headers.getFirst("X-Roles"))),
                "request", Map.of("method", exchange.getRequest().getMethod().name(), "path", "/v1/chat/completions"));
        return client.post()
                .uri("/v1/data/" + properties.getOpa().getDecisionPath())
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(Map.of("input", input))
                .retrieve()
                .bodyToMono(Map.class)
                .flatMap(document -> allowed(document)
                        ? chain.filter(exchange)
                        : deny(exchange))
                // A policy outage is not equivalent to an allow decision.
                .onErrorResume(error -> deny(exchange));
    }

    /**
     * 解析 OPA 响应中的 allow 决策；字段缺失或类型错误一律按拒绝处理。
    */
    private boolean allowed(Map<?, ?> document) {
        Object result = document.get("result");
        return Boolean.TRUE.equals(result)
                || result instanceof Map<?, ?> decision && Boolean.TRUE.equals(decision.get("allow"));
    }

    /**
     * 以统一 403 错误体终止未获 OPA 授权的请求，不继续进入控制器或模型调用。
    */
    private Mono<Void> deny(ServerWebExchange exchange) {
        exchange.getResponse().setStatusCode(HttpStatus.FORBIDDEN);
        exchange.getResponse().getHeaders().setContentType(MediaType.APPLICATION_JSON);
        byte[] body = "{\"error\":{\"code\":\"policy_denied\",\"message\":\"OPA policy denied request\"}}"
                .getBytes(StandardCharsets.UTF_8);
        return exchange.getResponse().writeWith(Mono.just(exchange.getResponse().bufferFactory().wrap(body)));
    }

    @Override
    /**
     * 读取当前配置或运行状态字段 get order 的值，供调用方进行受控决策。
    */
    public int getOrder() {
        return SecurityWebFiltersOrder.AUTHENTICATION.getOrder() + 2;
    }
}
