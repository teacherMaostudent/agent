package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;

import static org.assertj.core.api.Assertions.assertThat;

class TokenEstimatorTest {
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final TokenEstimator estimator = new TokenEstimator();

    @Test
    void estimatesPromptTokensFromMessages() throws Exception {
        JsonNode request = objectMapper.readTree("""
                {
                  "model": "gpt-4o-mini",
                  "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Explain RAG and Agent."}
                  ]
                }
                """);

        assertThat(estimator.estimatePromptTokens(request)).isPositive();
    }

    @Test
    void openAiTokenizerCountsChineseMoreAccuratelyThanCharacterQuarterRule() throws Exception {
        JsonNode request = objectMapper.readTree("""
                {
                  "model": "gpt-4o-mini",
                  "messages": [
                    {"role": "user", "content": "请检查这份企业流程文件是否缺少审批、分发、修订、归档和作废流程。"}
                  ]
                }
                """);
        ModelEndpoint endpoint = endpoint("openai", "gpt-4o-mini");

        long tokens = estimator.estimatePromptTokens(endpoint, request);
        long oldQuarterRule = request.path("messages").path(0).path("content").asText().length() / 4;

        assertThat(tokens).isGreaterThan(oldQuarterRule);
    }

    @Test
    void cjkAwareStrategyDoesNotCollapseChineseToQuarterLength() throws Exception {
        JsonNode request = objectMapper.readTree("""
                {
                  "model": "deepseek-v4-flash",
                  "messages": [
                    {"role": "user", "content": "质量管理文件缺少批准人和版本号。"}
                  ]
                }
                """);
        ModelEndpoint endpoint = endpoint("deepseek", "deepseek-v4-flash");

        long tokens = estimator.estimatePromptTokens(endpoint, request);

        assertThat(tokens).isGreaterThanOrEqualTo(14);
    }

    private ModelEndpoint endpoint(String providerName, String modelName) {
        GatewayProperties.Provider provider = new GatewayProperties.Provider();
        provider.setBaseUrl("https://example.com");
        provider.setProtocol("openai-compatible");
        GatewayProperties.Model model = new GatewayProperties.Model();
        model.setUpstreamModel(modelName);
        model.setInputPricePer1k(BigDecimal.ZERO);
        model.setOutputPricePer1k(BigDecimal.ZERO);
        return new ModelEndpoint(providerName, modelName, modelName, provider, model);
    }
}
