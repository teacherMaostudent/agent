package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.ModelEndpoint;

public interface OutputTokenPredictor {
    OutputTokenPrediction predict(ModelEndpoint endpoint, JsonNode request);

    /** 使用调用完成后的真实/最佳可得 usage 在线校准下一次预测。 */
    void observe(ModelEndpoint endpoint, JsonNode request, long actualCompletionTokens);
}
