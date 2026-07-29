package com.zxf.ai.gateway.routing;

import com.zxf.ai.gateway.config.GatewayProperties;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class ModelRouterTest {
    @Test
    void resolvesPrimaryAndFallbacks() {
        GatewayProperties properties = new GatewayProperties();
        GatewayProperties.Provider openai = new GatewayProperties.Provider();
        openai.setBaseUrl("https://api.openai.com/v1");
        GatewayProperties.Model mini = new GatewayProperties.Model();
        mini.setUpstreamModel("gpt-4o-mini");
        GatewayProperties.Model fallback = new GatewayProperties.Model();
        fallback.setUpstreamModel("gpt-4.1-mini");
        openai.setModels(Map.of("gpt-4o-mini", mini, "gpt-4.1-mini", fallback));
        properties.setProviders(Map.of("openai", openai));
        GatewayProperties.Route route = new GatewayProperties.Route();
        route.setPrimary("openai:gpt-4o-mini");
        route.setFallbacks(List.of("openai:gpt-4.1-mini"));
        properties.setRoutes(Map.of("default", route));

        ModelRouter router = new ModelRouter(properties);

        assertThat(router.resolve("default"))
                .extracting("upstreamModel")
                .containsExactly("gpt-4o-mini", "gpt-4.1-mini");
    }
}
