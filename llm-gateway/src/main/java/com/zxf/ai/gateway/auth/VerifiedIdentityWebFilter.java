package com.zxf.ai.gateway.auth;

import com.zxf.ai.gateway.config.GatewayProperties;
import org.springframework.core.Ordered;
import org.springframework.security.config.web.server.SecurityWebFiltersOrder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;
import reactor.core.publisher.Mono;

import java.util.Collection;
import java.util.List;

/**
 * Converts a verified OIDC principal into the internal identity headers used by
 * the gateway's existing controller and downstream-client contracts.
 *
 * <p>Caller-supplied tenant, user and role headers are always removed before
 * being reconstructed from JWT claims.  A verified platform workload may carry
 * delegated tenant/user context; an end-user token may not.</p>
 */
@Component
public class VerifiedIdentityWebFilter implements WebFilter, Ordered {
    private final GatewayProperties properties;

    public VerifiedIdentityWebFilter(GatewayProperties properties) {
        this.properties = properties;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        if (!properties.getOidc().isEnabled()) {
            return chain.filter(exchange);
        }
        return exchange.getPrincipal()
                .ofType(JwtAuthenticationToken.class)
                .flatMap(authentication -> {
                    var jwt = authentication.getToken();
                    Object rolesClaim = jwt.getClaims().get(properties.getOidc().getRolesClaim());
                    List<String> roleValues = rolesClaim instanceof Collection<?> values
                            ? values.stream().map(String::valueOf).toList()
                            : rolesClaim == null ? List.of()
                            : List.of(String.valueOf(rolesClaim).split(","));
                    boolean workload = roleValues.contains("platform-workload");
                    String tenant = workload
                            ? exchange.getRequest().getHeaders().getFirst("X-Tenant-Id")
                            : jwt.getClaimAsString(properties.getOidc().getTenantClaim());
                    if (tenant == null || tenant.isBlank()) {
                        return Mono.error(new IllegalArgumentException("OIDC tenant claim is required"));
                    }
                    String delegatedUser = exchange.getRequest().getHeaders().getFirst("X-User-Id");
                    String user = workload && delegatedUser != null && !delegatedUser.isBlank()
                            ? delegatedUser : jwt.getSubject();
                    String roles = String.join(",", roleValues);
                    // Downstream code is header-based for compatibility, but
                    // only values derived above from an authenticated JWT pass on.
                    ServerWebExchange trusted = exchange.mutate().request(builder -> builder.headers(headers -> {
                        headers.remove("X-Tenant-Id");
                        headers.remove("X-User-Id");
                        headers.remove("X-Roles");
                        headers.set("X-Tenant-Id", tenant);
                        headers.set("X-User-Id", user);
                        headers.set("X-Roles", roles);
                    })).build();
                    return chain.filter(trusted);
                })
                .switchIfEmpty(Mono.defer(() -> chain.filter(exchange)));
    }

    @Override
    public int getOrder() {
        return SecurityWebFiltersOrder.AUTHENTICATION.getOrder() + 1;
    }
}
