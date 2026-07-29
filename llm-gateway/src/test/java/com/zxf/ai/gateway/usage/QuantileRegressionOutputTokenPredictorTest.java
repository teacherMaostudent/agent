package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class QuantileRegressionOutputTokenPredictorTest {
    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void predictionShouldBeMonotonicAndRespectExplicitLimit() throws Exception {
        GatewayProperties properties = new GatewayProperties();
        TokenEstimator estimator = new TokenEstimator();
        QuantileRegressionOutputTokenPredictor predictor =
                new QuantileRegressionOutputTokenPredictor(properties, estimator);
        JsonNode request = mapper.readTree("""
                {"model":"test","max_tokens":1000,"messages":[{"role":"user","content":"解释网关费用治理"}]}
                """);

        OutputTokenPrediction prediction = predictor.predict(endpoint(), request);

        assertThat(prediction.p50()).isLessThanOrEqualTo(prediction.p90());
        assertThat(prediction.p90()).isLessThanOrEqualTo(prediction.p95());
        assertThat(prediction.p95()).isLessThanOrEqualTo(prediction.p99());
        assertThat(prediction.selected()).isLessThanOrEqualTo(1000);
    }

    @Test
    void onlinePinballLearningShouldReactToLongerActualOutputs() throws Exception {
        GatewayProperties properties = new GatewayProperties();
        properties.getCostPrediction().setMinSamples(5);
        properties.getCostPrediction().setLearningRate(5.0);
        QuantileRegressionOutputTokenPredictor predictor =
                new QuantileRegressionOutputTokenPredictor(properties, new TokenEstimator());
        JsonNode request = mapper.readTree("""
                {"model":"test","messages":[{"role":"user","content":"生成完整合规报告"}]}
                """);
        long before = predictor.predict(endpoint(), request).p95();

        for (int i = 0; i < 20; i++) predictor.observe(endpoint(), request, 8000);
        OutputTokenPrediction after = predictor.predict(endpoint(), request);

        assertThat(after.p95()).isGreaterThan(before);
        assertThat(after.sampleCount()).isEqualTo(20);
        assertThat(after.conformalCorrection()).isGreaterThan(0);
    }

    @Test
    void disabledPredictorShouldUseStaticColdStartAndIgnoreObservations() throws Exception {
        GatewayProperties properties = new GatewayProperties();
        properties.getCostPrediction().setEnabled(false);
        QuantileRegressionOutputTokenPredictor predictor =
                new QuantileRegressionOutputTokenPredictor(properties, new TokenEstimator());
        JsonNode request = mapper.readTree("""
                {"model":"test","messages":[{"role":"user","content":"test"}]}
                """);

        predictor.observe(endpoint(), request, 9000);
        OutputTokenPrediction prediction = predictor.predict(endpoint(), request);

        assertThat(prediction.p95()).isEqualTo(properties.getCostPrediction().getColdStartP95());
        assertThat(prediction.sampleCount()).isZero();
        assertThat(prediction.modelVersion()).isEqualTo("cold-start-static-v1");
    }

    private ModelEndpoint endpoint() {
        GatewayProperties.Provider provider = new GatewayProperties.Provider();
        provider.setBaseUrl("http://localhost");
        GatewayProperties.Model model = new GatewayProperties.Model();
        model.setUpstreamModel("test-model");
        model.setInputPricePer1k(new BigDecimal("0.001"));
        model.setOutputPricePer1k(new BigDecimal("0.002"));
        provider.setModels(Map.of("test", model));
        return new ModelEndpoint("test-provider", "test", "test-model", provider, model);
    }
}
