package com.zxf.ai.gateway.config;

import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ProductionConfigurationValidatorTest {
    @Test
    void productionRejectsLocalDefaults() {
        GatewayProperties properties = new GatewayProperties();
        MockEnvironment environment = new MockEnvironment()
                .withProperty("DEPLOYMENT_ENVIRONMENT", "production");

        assertThatThrownBy(() -> new ProductionConfigurationValidator(properties, environment)
                .afterPropertiesSet())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("Unsafe production configuration");
    }
}
