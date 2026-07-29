package com.zxf.ai.gateway.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.web.server.ServerHttpSecurity;
import org.springframework.security.core.userdetails.MapReactiveUserDetailsService;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.crypto.factory.PasswordEncoderFactories;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.server.SecurityWebFilterChain;

@Configuration
public class SecurityConfig {
    private final GatewayProperties properties;

    public SecurityConfig(GatewayProperties properties) {
        this.properties = properties;
    }

    @Bean
    public SecurityWebFilterChain securityWebFilterChain(ServerHttpSecurity http) {
        GatewayProperties.Security security = properties.getAdmin().getSecurity();
        if (security.isEnabled()) {
            return http
                    .csrf(ServerHttpSecurity.CsrfSpec::disable)
                    .authorizeExchange(exchanges -> exchanges
                            .pathMatchers("/admin/**").hasRole("ADMIN")
                            .pathMatchers(HttpMethod.GET, "/swagger-ui/**", "/v3/api-docs/**").permitAll()
                            .pathMatchers(HttpMethod.GET, "/actuator/health", "/actuator/info", "/actuator/prometheus").permitAll()
                            .pathMatchers("/v1/**").permitAll()
                            .anyExchange().permitAll())
                    .httpBasic(Customizer.withDefaults())
                    .build();
        }

        return http
                .csrf(ServerHttpSecurity.CsrfSpec::disable)
                .authorizeExchange(exchanges -> exchanges.anyExchange().permitAll())
                .build();
    }

    @Bean
    public MapReactiveUserDetailsService adminUsers(PasswordEncoder passwordEncoder) {
        GatewayProperties.Security security = properties.getAdmin().getSecurity();
        return new MapReactiveUserDetailsService(User
                .withUsername(security.getUsername())
                .password(passwordEncoder.encode(security.getPassword()))
                .roles("ADMIN")
                .build());
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return PasswordEncoderFactories.createDelegatingPasswordEncoder();
    }
}
