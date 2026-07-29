package com.zxf.ai.gateway.usage;

/**
 * 单次请求的输出 token 条件分位数预测。
 *
 * <p>预测值只用于调用前额度预留；模型返回后必须用厂商 usage 或 tokenizer 结果结算。</p>
 */
public record OutputTokenPrediction(
        long p50,
        long p90,
        long p95,
        long p99,
        long selected,
        long conformalCorrection,
        long sampleCount,
        String modelVersion
) {
}
