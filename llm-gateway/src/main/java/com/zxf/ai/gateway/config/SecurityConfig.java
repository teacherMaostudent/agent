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

    /**
     * 初始化 security config 所需的依赖与运行期状态。
    */
    public SecurityConfig(GatewayProperties properties) {
        this.properties = properties;
    }

    @Bean
    /**
     * 配置 OIDC、管理端 Basic Auth、CSRF 与端点授权顺序，未匹配路径默认拒绝。
    */
    public SecurityWebFilterChain securityWebFilterChain(ServerHttpSecurity http) {
        GatewayProperties.Security security = properties.getAdmin().getSecurity();
        boolean oidcEnabled = properties.getOidc().isEnabled();
        if (security.isEnabled()) {
            ServerHttpSecurity configured = http
                    .csrf(ServerHttpSecurity.CsrfSpec::disable)
                    .authorizeExchange(exchanges -> {
                        exchanges.pathMatchers("/admin/**").hasRole("ADMIN")
                                .pathMatchers(HttpMethod.GET, "/swagger-ui/**", "/v3/api-docs/**").permitAll()
                                .pathMatchers(HttpMethod.GET, "/actuator/health", "/actuator/info", "/actuator/prometheus").permitAll();
                        if (oidcEnabled) {
                            exchanges.pathMatchers("/v1/chat/completions").permitAll()
                                    .pathMatchers("/v1/**").authenticated();
                        } else {
                            exchanges.pathMatchers("/v1/**").permitAll();
                        }
                        exchanges.anyExchange().permitAll();
                    })
                    .httpBasic(Customizer.withDefaults());
            if (oidcEnabled) {
                configured.oauth2ResourceServer(resource -> resource.jwt(Customizer.withDefaults()));
            }
            return configured.build();
        }

        return http
                .csrf(ServerHttpSecurity.CsrfSpec::disable)
                .authorizeExchange(exchanges -> exchanges.anyExchange().permitAll())
                .build();
    }

    @Bean
    /**
     * 从受控配置创建仅用于管理端点的响应式用户仓储，并使用已注入编码器处理密码。
    */
    public MapReactiveUserDetailsService adminUsers(PasswordEncoder passwordEncoder) {
        GatewayProperties.Security security = properties.getAdmin().getSecurity();
        return new MapReactiveUserDetailsService(User
                .withUsername(security.getUsername())
                .password(passwordEncoder.encode(security.getPassword()))
                .roles("ADMIN")
                .build());
    }

    @Bean
    /**
     * 提供单向自适应密码编码器，禁止以明文比较管理用户密码。
    */
    public PasswordEncoder passwordEncoder() {
        return PasswordEncoderFactories.createDelegatingPasswordEncoder();
    }
}
