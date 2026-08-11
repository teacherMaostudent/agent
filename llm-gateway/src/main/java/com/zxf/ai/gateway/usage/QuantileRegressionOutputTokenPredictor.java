package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.stereotype.Component;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 轻量在线条件分位数回归器。
 *
 * <p>每个 provider:model 独立维护 P50/P90/P95/P99 四个线性分位数模型，采用 pinball loss
 * 的随机梯度在线更新；P95 再使用近期超额残差做 conformal 校准。它不依赖离线模型文件，
 * 适合作为项目冷启动方案。数据量增长后可保持接口不变，替换为 LightGBM/ONNX 实现。</p>
 */
@Component
public class QuantileRegressionOutputTokenPredictor implements OutputTokenPredictor {
    private static final double[] QUANTILES = {0.50, 0.90, 0.95, 0.99};
    private static final String VERSION = "online-linear-quantile-v1";

    private final GatewayProperties properties;
    private final TokenEstimator tokenEstimator;
    private final Map<String, ModelState> states = new ConcurrentHashMap<>();

    /** 注入预测参数与统一 Token 估算器，状态仍按 provider:model 隔离保存。 */
    public QuantileRegressionOutputTokenPredictor(GatewayProperties properties, TokenEstimator tokenEstimator) {
        this.properties = properties;
        this.tokenEstimator = tokenEstimator;
    }

    @Override
    /** 返回冷启动或在线分位回归预测，并选择满足预算风险偏好的预占分位数。 */
    public OutputTokenPrediction predict(ModelEndpoint endpoint, JsonNode request) {
        GatewayProperties.CostPrediction config = properties.getCostPrediction();
        if (!config.isEnabled()) {
            return coldStartPrediction(config, request);
        }
        double[] features = features(endpoint, request);
        ModelState state = states.computeIfAbsent(endpoint.key(), ignored -> newState(config));
        synchronized (state) {
            long[] raw = state.predict(features);
            enforceMonotonic(raw);
            long correction = state.samples >= config.getMinSamples()
                    ? conformalCorrection(state.p95Residuals, 0.95)
                    : 0;
            long correctedP95 = safeAdd(raw[2], correction);
            long selected = select(config.getReservationQuantile(), raw[0], raw[1], correctedP95, raw[3]);

            // 调用方明确给出 max_tokens/max_completion_tokens 时，它是业务允许的硬上限；
            // 未提供时不擅自注入较小上限，避免为了费用预测截断正常回答。
            long explicitLimit = explicitOutputLimit(request);
            if (explicitLimit > 0) {
                selected = Math.min(selected, explicitLimit);
            }
            return new OutputTokenPrediction(raw[0], raw[1], correctedP95, raw[3],
                    Math.max(1, selected), correction, state.samples, VERSION);
        }
    }

    @Override
    /** 用完成响应的实际输出长度更新该模型的分位回归和 P95 残差窗口。 */
    public void observe(ModelEndpoint endpoint, JsonNode request, long actualCompletionTokens) {
        if (!properties.getCostPrediction().isEnabled() || actualCompletionTokens < 0) {
            return;
        }
        GatewayProperties.CostPrediction config = properties.getCostPrediction();
        double[] features = features(endpoint, request);
        ModelState state = states.computeIfAbsent(endpoint.key(), ignored -> newState(config));
        synchronized (state) {
            long p95BeforeUpdate = state.predict(features)[2];
            state.addResidual(actualCompletionTokens - p95BeforeUpdate, config.getConformalWindow());
            state.update(features, actualCompletionTokens, config.getLearningRate());
            state.samples++;
        }
    }

    /**
     * 关闭在线预测时仍返回一个静态冷启动预算，保证配额预占链路不会退回到“只计算输入”的不安全状态。
     */
    /** 在线预测关闭或样本不足时返回配置化冷启动分位数，仍保留安全预算。 */
    private OutputTokenPrediction coldStartPrediction(GatewayProperties.CostPrediction config, JsonNode request) {
        long p50 = Math.max(1, config.getColdStartP50());
        long p90 = Math.max(p50, config.getColdStartP90());
        long p95 = Math.max(p90, config.getColdStartP95());
        long p99 = Math.max(p95, config.getColdStartP99());
        long selected = select(config.getReservationQuantile(), p50, p90, p95, p99);
        long explicitLimit = explicitOutputLimit(request);
        if (explicitLimit > 0) {
            selected = Math.min(selected, explicitLimit);
        }
        return new OutputTokenPrediction(p50, p90, p95, p99,
                Math.max(1, selected), 0, 0, "cold-start-static-v1");
    }

    /** 用冷启动分位数初始化线性模型权重，避免初始预测为零。 */
    private ModelState newState(GatewayProperties.CostPrediction config) {
        long[] cold = {config.getColdStartP50(), config.getColdStartP90(),
                config.getColdStartP95(), config.getColdStartP99()};
        double[][] weights = new double[QUANTILES.length][Feature.SIZE];
        for (int i = 0; i < weights.length; i++) {
            weights[i][Feature.BIAS] = cold[i];
            weights[i][Feature.INPUT_LOG] = 64.0;
            weights[i][Feature.RAG_CONTEXT] = 32.0;
            weights[i][Feature.TOOL_COUNT] = 32.0;
            weights[i][Feature.STRUCTURED] = 128.0;
        }
        return new ModelState(weights);
    }

    /** 从提示词规模、消息、工具、RAG、结构化输出与温度构造稳定特征向量。 */
    private double[] features(ModelEndpoint endpoint, JsonNode request) {
        long inputTokens = tokenEstimator.estimatePromptTokens(endpoint, request);
        int messageCount = request.path("messages").isArray() ? request.path("messages").size() : 1;
        int toolCount = request.path("tools").isArray() ? request.path("tools").size() : 0;
        long ragTokens = request.path("rag_context_tokens").asLong(0);
        boolean structured = request.has("response_format") || request.has("json_schema");
        return new double[]{
                1.0,
                Math.log1p(inputTokens),
                Math.min(10.0, messageCount / 4.0),
                Math.min(10.0, toolCount),
                Math.log1p(Math.max(0, ragTokens)),
                structured ? 1.0 : 0.0,
                Math.max(0.0, Math.min(2.0, request.path("temperature").asDouble(0.7)))
        };
    }

    /** 读取调用方显式声明的最大输出长度；未声明时返回零表示不施加硬上限。 */
    private long explicitOutputLimit(JsonNode request) {
        long maxCompletion = request.path("max_completion_tokens").asLong(0);
        return maxCompletion > 0 ? maxCompletion : request.path("max_tokens").asLong(0);
    }

    /** 将配置的风险分位数映射到对应预测值，保证选择结果可审计。 */
    private long select(double quantile, long p50, long p90, long p95, long p99) {
        if (quantile <= 0.50) return p50;
        if (quantile <= 0.90) return p90;
        if (quantile <= 0.95) return p95;
        return p99;
    }

    /** 修复数值训练造成的分位数倒置，保持 P50 至 P99 单调不减。 */
    private void enforceMonotonic(long[] values) {
        for (int i = 1; i < values.length; i++) {
            values[i] = Math.max(values[i], values[i - 1]);
        }
    }

    /** 从近期残差取目标覆盖率分位数，给 P95 预测增加非负校准余量。 */
    private long conformalCorrection(Deque<Long> residuals, double coverage) {
        if (residuals.isEmpty()) return 0;
        long[] sorted = residuals.stream().mapToLong(Long::longValue).sorted().toArray();
        int index = Math.min(sorted.length - 1, Math.max(0, (int) Math.ceil(coverage * sorted.length) - 1));
        return Math.max(0, sorted[index]);
    }

    /** 在加入校准余量时防止长整型溢出，溢出则饱和到最大值。 */
    private long safeAdd(long value, long delta) {
        if (Long.MAX_VALUE - value < delta) return Long.MAX_VALUE;
        return value + delta;
    }

    private static final class Feature {
        static final int BIAS = 0;
        static final int INPUT_LOG = 1;
        static final int MESSAGE_COUNT = 2;
        static final int TOOL_COUNT = 3;
        static final int RAG_CONTEXT = 4;
        static final int STRUCTURED = 5;
        static final int TEMPERATURE = 6;
        static final int SIZE = 7;
    }

    private static final class ModelState {
        private final double[][] weights;
        private final Deque<Long> p95Residuals = new ArrayDeque<>();
        private long samples;

        /** 保存四个分位模型的可变权重与有限长度的 P95 残差窗口。 */
        private ModelState(double[][] weights) {
            this.weights = weights;
        }

        /** 对给定特征计算四个线性分位预测，并将每项下限限定为一个 Token。 */
        long[] predict(double[] features) {
            long[] result = new long[weights.length];
            for (int q = 0; q < weights.length; q++) {
                double value = 0;
                for (int i = 0; i < features.length; i++) value += weights[q][i] * features[i];
                result[q] = Math.max(1, Math.round(value));
            }
            return result;
        }

        /** 使用 pinball-loss 方向更新每个分位模型，避免依赖离线训练工件。 */
        void update(double[] features, long actual, double learningRate) {
            long[] predicted = predict(features);
            for (int q = 0; q < weights.length; q++) {
                double direction = actual >= predicted[q] ? QUANTILES[q] : -(1.0 - QUANTILES[q]);
                for (int i = 0; i < features.length; i++) {
                    weights[q][i] += learningRate * direction * features[i];
                }
            }
        }

        /** 记录 P95 残差并裁剪窗口，限制单模型的在线状态内存。 */
        void addResidual(long residual, int maxWindow) {
            p95Residuals.addLast(residual);
            while (p95Residuals.size() > Math.max(10, maxWindow)) p95Residuals.removeFirst();
        }
    }
}
