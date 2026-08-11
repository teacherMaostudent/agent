package com.zxf.ai.gateway.rag;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "rag-agent")
public class RagAgentProperties {
    private boolean enabled = true;
    private String baseUrl = "http://localhost:8000";
    private String apiKey = "dev-rag-key";
    private Duration connectTimeout = Duration.ofSeconds(3);
    private Duration responseTimeout = Duration.ofSeconds(60);
    private String permissions = "rag:read,document:read";

    /** 返回 RAG 集成是否由部署配置启用。 */
    public boolean isEnabled() {
        return enabled;
    }

    /** 写入 RAG 集成开关，供 Spring 配置绑定调用。 */
    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    /** 返回 RAG 服务基地址。 */
    public String getBaseUrl() {
        return baseUrl;
    }

    /** 写入 RAG 服务基地址。 */
    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    /** 返回过渡服务密钥；生产环境应由工作负载身份替代。 */
    public String getApiKey() {
        return apiKey;
    }

    /** 写入过渡服务密钥，仅供安全配置绑定。 */
    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    /** 返回建立 RAG 连接的最长等待时间。 */
    public Duration getConnectTimeout() {
        return connectTimeout;
    }

    /** 写入建立 RAG 连接的最长等待时间。 */
    public void setConnectTimeout(Duration connectTimeout) {
        this.connectTimeout = connectTimeout;
    }

    /** 返回 RAG 响应允许的最长时间。 */
    public Duration getResponseTimeout() {
        return responseTimeout;
    }

    /** 写入 RAG 响应允许的最长时间。 */
    public void setResponseTimeout(Duration responseTimeout) {
        this.responseTimeout = responseTimeout;
    }

    /** 返回向 RAG 传播的受控服务权限集合。 */
    public String getPermissions() {
        return permissions;
    }

    /** 写入向 RAG 传播的受控服务权限集合。 */
    public void setPermissions(String permissions) {
        this.permissions = permissions;
    }
}
