package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

class CostCalculatorTest {
    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void shouldApplyCachePriceTierAndCurrencyConversion() throws Exception {
        GatewayProperties properties = new GatewayProperties();
        properties.getBilling().setExchangeRates(java.util.Map.of(
                "CNY", BigDecimal.ONE,
                "USD", new BigDecimal("7.20")));
        GatewayProperties.Provider provider = new GatewayProperties.Provider();
        provider.setBaseUrl("https://example.com");
        GatewayProperties.Model model = new GatewayProperties.Model();
        model.setUpstreamModel("priced-model");
        model.setCurrency("USD");
        model.setInputPricePer1k(new BigDecimal("0.010"));
        model.setCachedInputPricePer1k(new BigDecimal("0.001"));
        model.setOutputPricePer1k(new BigDecimal("0.020"));

        GatewayProperties.PriceTier tier = new GatewayProperties.PriceTier();
        tier.setMaxInputTokens(10_000);
        tier.setInputPricePer1k(new BigDecimal("0.012"));
        tier.setCachedInputPricePer1k(new BigDecimal("0.002"));
        tier.setOutputPricePer1k(new BigDecimal("0.024"));
        model.setPriceTiers(List.of(tier));
        ModelEndpoint endpoint = new ModelEndpoint("provider", "model", "priced-model", provider, model);

        BigDecimal cost = new CostCalculator(properties).estimate(endpoint,
                mapper.readTree("{\"usage\":{\"prompt_tokens\":1000,\"prompt_tokens_details\":{\"cached_tokens\":600},\"completion_tokens\":500}}"),
                1000, 500);

        // Native USD: 400*0.012/1k + 600*0.002/1k + 500*0.024/1k = 0.018 USD; then * 7.20 CNY/USD.
        assertThat(cost).isEqualByComparingTo(new BigDecimal("0.1296"));
    }
}
