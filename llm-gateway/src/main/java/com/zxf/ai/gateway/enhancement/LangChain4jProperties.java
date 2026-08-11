package com.zxf.ai.gateway.enhancement;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

/**
 * LangChain4j 增强层配置。
 *
 * <p>这组配置和 gateway.providers 分开，是为了强调两层职责不同：
 * gateway 主链路负责统一转发、路由和治理；enhancement 增强层负责 RAG、Tool Calling、
 * Agent 编排等上层智能能力。</p>
 */
@ConfigurationProperties(prefix = "enhancement.langchain4j")
public class LangChain4jProperties {
    private boolean enabled = true;
    private String baseUrl = "https://api.deepseek.com";
    private String apiKey = "";
    private String modelName = "deepseek-v4-flash";
    private Duration timeout = Duration.ofSeconds(60);

    /** 返回增强层是否由部署配置启用。 */
    public boolean isEnabled() {
        return enabled;
    }

    /** 写入增强层开关，供 Spring 配置绑定调用。 */
    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    /** 返回上游兼容模型服务地址。 */
    public String getBaseUrl() {
        return baseUrl;
    }

    /** 写入上游兼容模型服务地址。 */
    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    /** 返回配置绑定后的上游服务凭据；调用方不得记录其值。 */
    public String getApiKey() {
        return apiKey;
    }

    /** 写入上游服务凭据，仅供安全配置绑定过程使用。 */
    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    /** 返回增强层默认逻辑模型名称。 */
    public String getModelName() {
        return modelName;
    }

    /** 写入增强层默认逻辑模型名称。 */
    public void setModelName(String modelName) {
        this.modelName = modelName;
    }

    /** 返回单次增强模型调用的超时上限。 */
    public Duration getTimeout() {
        return timeout;
    }

    /** 写入单次增强模型调用的超时上限。 */
    public void setTimeout(Duration timeout) {
        this.timeout = timeout;
    }
}
