package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.ModelEndpoint;

/** 为调用前额度预占提供模型输出长度预测的统一边界。 */
public interface OutputTokenPredictor {
    /** 根据模型、提示词和工具上下文预测输出 Token 分位数，供调用前预算预占。 */
    OutputTokenPrediction predict(ModelEndpoint endpoint, JsonNode request);

    /** 使用已完成调用的真实或最佳可得用量，在线校准后续预测。 */
    void observe(ModelEndpoint endpoint, JsonNode request, long actualCompletionTokens);
}
