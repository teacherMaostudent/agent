package com.zxf.ai.gateway.routing;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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

    @Test
    void rejectsPinnedRouteWhenReleaseOrModelRevisionDrifts() {
        GatewayProperties properties = new GatewayProperties();
        GatewayProperties.Provider provider = new GatewayProperties.Provider();
        provider.setBaseUrl("https://api.openai.com/v1");
        GatewayProperties.Model model = new GatewayProperties.Model();
        model.setUpstreamModel("gpt-4o-mini");
        model.setRevision("provider-snapshot-2026-08-01");
        provider.setModels(Map.of("judge", model));
        properties.setProviders(Map.of("openai", provider));
        GatewayProperties.Route route = new GatewayProperties.Route();
        route.setPrimary("openai:judge");
        route.setVersion("governance-evaluation-v1");
        properties.setRoutes(Map.of("governance-judge", route));

        ModelRouter router = new ModelRouter(properties);

        assertThat(router.resolvePinned(
                "governance-judge", "governance-evaluation-v1", "provider-snapshot-2026-08-01"))
                .hasSize(1);
        assertThatThrownBy(() -> router.resolvePinned(
                "governance-judge", "governance-evaluation-v2", "provider-snapshot-2026-08-01"))
                .isInstanceOf(GatewayException.class);
        assertThatThrownBy(() -> router.resolvePinned(
                "governance-judge", "governance-evaluation-v1", "provider-snapshot-other"))
                .isInstanceOf(GatewayException.class);
    }
}
