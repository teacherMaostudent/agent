package com.zxf.ai.gateway.auth;

import com.zxf.ai.gateway.config.GatewayProperties;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class ApiKeyServiceTest {
    @Test
    void trustedServiceCredentialCarriesTheEndUserTenantWithoutLosingModelPolicy() {
        GatewayProperties properties = new GatewayProperties();
        GatewayProperties.ApiKey key = new GatewayProperties.ApiKey();
        key.setTenantId("internal");
        key.setUserId("agent-governance");
        key.setTrustedService(true);
        key.setAllowedModels(List.of("judge-model"));
        properties.setApiKeys(Map.of("service-key", key));

        ApiKeyService.AuthResult result = new ApiKeyService(properties).authenticate(
                null, "service-key", "tenant-a", "end-user", "judge-model");

        assertThat(result.tenantId()).isEqualTo("tenant-a");
        assertThat(result.userId()).isEqualTo("agent-governance");
        assertThat(result.authenticated()).isTrue();
    }
}
