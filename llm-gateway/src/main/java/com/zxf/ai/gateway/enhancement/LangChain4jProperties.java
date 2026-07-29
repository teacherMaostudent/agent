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

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getModelName() {
        return modelName;
    }

    public void setModelName(String modelName) {
        this.modelName = modelName;
    }

    public Duration getTimeout() {
        return timeout;
    }

    public void setTimeout(Duration timeout) {
        this.timeout = timeout;
    }
}
