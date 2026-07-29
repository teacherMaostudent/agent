package com.zxf.ai.gateway.model;

import com.zxf.ai.gateway.config.GatewayProperties;

/**
 * 已解析完成的模型调用端点。
 *
 * <p>它把 provider、网关内部模型名、上游真实模型名和价格配置放在一起，
 * 供客户端调用、日志记录和成本统计使用。</p>
 */
public record ModelEndpoint(
        String providerName,
        String modelName,
        String upstreamModel,
        GatewayProperties.Provider provider,
        GatewayProperties.Model model
) {
    /**
     * 统一的路由标识，日志和错误信息里都使用 provider:model 格式。
     */
    public String key() {
        return providerName + ":" + modelName;
    }
}
