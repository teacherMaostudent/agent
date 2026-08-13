package com.zxf.ai.gateway.config;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.math.BigDecimal;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Validated
@ConfigurationProperties(prefix = "gateway")
public class GatewayProperties {
    /**
     * 上游模型单次调用超时时间，WebClient 会在 client 层统一使用。
     */
    @NotNull
    private Duration requestTimeout = Duration.ofSeconds(30);
    /**
     * 单个上游请求的最大重试次数。fallback 是换路由，retry 是同一路由再试一次。
     */
    private int maxRetries = 2;
    /**
     * quotaStore=memory 适合本地演示，quotaStore=redis 适合多实例部署。
     */
    private String quotaStore = "memory";
    private boolean allowAnonymous = true;
    @NotBlank
    private String defaultModel = "gpt-4o-mini";
    /**
     * provider 表示真实模型服务厂商或本地推理服务，例如 deepseek/openai/claude/ollama/vllm。
     */
    private Map<String, Provider> providers = new LinkedHashMap<>();
    /**
     * route 表示业务侧可见的逻辑模型名，可以映射到 primary、fallback、灰度和权重目标。
     */
    private Map<String, Route> routes = new LinkedHashMap<>();
    private Map<String, UserQuota> userQuotas = new LinkedHashMap<>();
    private Map<String, ApiKey> apiKeys = new LinkedHashMap<>();
    private Cache cache = new Cache();
    private Resilience resilience = new Resilience();
    /** 实时准入控制独立于熔断和每日配额，生产副本共享 Redis 状态。 */
    private Admission admission = new Admission();
    private Persistence persistence = new Persistence();
    private Admin admin = new Admin();
    private Oidc oidc = new Oidc();
    private Opa opa = new Opa();
    /** Migration switches for capabilities that no longer belong to the Gateway. */
    private Compatibility compatibility = new Compatibility();
    /**
     * 输出 token 预测与调用前费用预留策略。
     */
    private CostPrediction costPrediction = new CostPrediction();
    /** 原生价格统一换算到报表基准币种。汇率必须带版本并由运维定期更新。 */
    private Billing billing = new Billing();
    private Map<String, PromptTemplate> promptTemplates = new LinkedHashMap<>();

    /** 返回请求频率、Token 吞吐、并发和载荷上限配置。 */
    public Admission getAdmission() {
        return admission;
    }

    /** 由 Spring 绑定受控的实时准入配置。 */
    public void setAdmission(Admission admission) {
        this.admission = admission;
    }

    /**
     * 读取当前配置或运行状态字段 get oidc 的值，供调用方进行受控决策。
    */
    public Oidc getOidc() {
        return oidc;
    }

    /**
     * 更新配置字段 set oidc；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setOidc(Oidc oidc) {
        this.oidc = oidc;
    }

    /**
     * 读取当前配置或运行状态字段 get opa 的值，供调用方进行受控决策。
    */
    public Opa getOpa() {
        return opa;
    }

    /**
     * 更新配置字段 set opa；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setOpa(Opa opa) {
        this.opa = opa;
    }

    /**
     * 读取当前配置或运行状态字段 get request timeout 的值，供调用方进行受控决策。
    */
    public Duration getRequestTimeout() {
        return requestTimeout;
    }

    /**
     * 更新配置字段 set request timeout；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setRequestTimeout(Duration requestTimeout) {
        this.requestTimeout = requestTimeout;
    }

    /**
     * 读取当前配置或运行状态字段 get max retries 的值，供调用方进行受控决策。
    */
    public int getMaxRetries() {
        return maxRetries;
    }

    /**
     * 更新配置字段 set max retries；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setMaxRetries(int maxRetries) {
        this.maxRetries = maxRetries;
    }

    /**
     * 读取当前配置或运行状态字段 get quota store 的值，供调用方进行受控决策。
    */
    public String getQuotaStore() {
        return quotaStore;
    }

    /**
     * 更新配置字段 set quota store；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setQuotaStore(String quotaStore) {
        this.quotaStore = quotaStore;
    }

    /**
     * 读取当前配置或运行状态字段 is allow anonymous 的值，供调用方进行受控决策。
    */
    public boolean isAllowAnonymous() {
        return allowAnonymous;
    }

    /**
     * 更新配置字段 set allow anonymous；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setAllowAnonymous(boolean allowAnonymous) {
        this.allowAnonymous = allowAnonymous;
    }

    /**
     * 读取当前配置或运行状态字段 get compatibility 的值，供调用方进行受控决策。
    */
    public Compatibility getCompatibility() {
        return compatibility;
    }

    /**
     * 更新配置字段 set compatibility；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setCompatibility(Compatibility compatibility) {
        this.compatibility = compatibility;
    }

    /**
     * 读取当前配置或运行状态字段 get default model 的值，供调用方进行受控决策。
    */
    public String getDefaultModel() {
        return defaultModel;
    }

    /**
     * 更新配置字段 set default model；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setDefaultModel(String defaultModel) {
        this.defaultModel = defaultModel;
    }

    /**
     * 读取当前配置或运行状态字段 get providers 的值，供调用方进行受控决策。
    */
    public Map<String, Provider> getProviders() {
        return providers;
    }

    /**
     * 更新配置字段 set providers；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setProviders(Map<String, Provider> providers) {
        this.providers = providers;
    }

    /**
     * 读取当前配置或运行状态字段 get routes 的值，供调用方进行受控决策。
    */
    public Map<String, Route> getRoutes() {
        return routes;
    }

    /**
     * 更新配置字段 set routes；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setRoutes(Map<String, Route> routes) {
        this.routes = routes;
    }

    /**
     * 读取当前配置或运行状态字段 get user quotas 的值，供调用方进行受控决策。
    */
    public Map<String, UserQuota> getUserQuotas() {
        return userQuotas;
    }

    /**
     * 更新配置字段 set user quotas；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setUserQuotas(Map<String, UserQuota> userQuotas) {
        this.userQuotas = userQuotas;
    }

    /**
     * 读取当前配置或运行状态字段 get api keys 的值，供调用方进行受控决策。
    */
    public Map<String, ApiKey> getApiKeys() {
        return apiKeys;
    }

    /**
     * 更新配置字段 set api keys；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setApiKeys(Map<String, ApiKey> apiKeys) {
        this.apiKeys = apiKeys;
    }

    /**
     * 读取当前配置或运行状态字段 get cache 的值，供调用方进行受控决策。
    */
    public Cache getCache() {
        return cache;
    }

    /**
     * 更新配置字段 set cache；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setCache(Cache cache) {
        this.cache = cache;
    }

    /**
     * 读取当前配置或运行状态字段 get resilience 的值，供调用方进行受控决策。
    */
    public Resilience getResilience() {
        return resilience;
    }

    /**
     * 更新配置字段 set resilience；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setResilience(Resilience resilience) {
        this.resilience = resilience;
    }

    /**
     * 读取当前配置或运行状态字段 get persistence 的值，供调用方进行受控决策。
    */
    public Persistence getPersistence() {
        return persistence;
    }

    /**
     * 更新配置字段 set persistence；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setPersistence(Persistence persistence) {
        this.persistence = persistence;
    }

    /**
     * 读取当前配置或运行状态字段 get admin 的值，供调用方进行受控决策。
    */
    public Admin getAdmin() {
        return admin;
    }

    /**
     * 更新配置字段 set admin；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setAdmin(Admin admin) {
        this.admin = admin;
    }

    /**
     * 读取当前配置或运行状态字段 get cost prediction 的值，供调用方进行受控决策。
    */
    public CostPrediction getCostPrediction() {
        return costPrediction;
    }

    /**
     * 更新配置字段 set cost prediction；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setCostPrediction(CostPrediction costPrediction) {
        this.costPrediction = costPrediction;
    }

    /**
     * 读取当前配置或运行状态字段 get billing 的值，供调用方进行受控决策。
    */
    public Billing getBilling() { return billing; }
    /**
     * 更新配置字段 set billing；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setBilling(Billing billing) { this.billing = billing; }

    /**
     * 读取当前配置或运行状态字段 get prompt templates 的值，供调用方进行受控决策。
    */
    public Map<String, PromptTemplate> getPromptTemplates() {
        return promptTemplates;
    }

    /**
     * 更新配置字段 set prompt templates；该值由 Spring 配置绑定或受控管理接口提供。
    */
    public void setPromptTemplates(Map<String, PromptTemplate> promptTemplates) {
        this.promptTemplates = promptTemplates;
    }

    public static class Provider {
        /**
         * 上游服务地址，OpenAI-compatible provider 一般形如 https://xxx/v1。
         */
        @NotBlank
        private String baseUrl;
        /**
         * 协议类型：openai-compatible 或 anthropic。
         */
        private String protocol = "openai-compatible";
        private String apiKey;
        /**
         * 网关内模型名 -> 上游真实模型配置。
         */
        private Map<String, Model> models = new LinkedHashMap<>();

        /**
         * 读取当前配置或运行状态字段 get base url 的值，供调用方进行受控决策。
        */
        public String getBaseUrl() {
            return baseUrl;
        }

        /**
         * 更新配置字段 set base url；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setBaseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
        }

        /**
         * 读取当前配置或运行状态字段 get protocol 的值，供调用方进行受控决策。
        */
        public String getProtocol() {
            return protocol;
        }

        /**
         * 更新配置字段 set protocol；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setProtocol(String protocol) {
            this.protocol = protocol;
        }

        /**
         * 读取当前配置或运行状态字段 get api key 的值，供调用方进行受控决策。
        */
        public String getApiKey() {
            return apiKey;
        }

        /**
         * 更新配置字段 set api key；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setApiKey(String apiKey) {
            this.apiKey = apiKey;
        }

        /**
         * 读取当前配置或运行状态字段 get models 的值，供调用方进行受控决策。
        */
        public Map<String, Model> getModels() {
            return models;
        }

        /**
         * 更新配置字段 set models；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setModels(Map<String, Model> models) {
            this.models = models;
        }
    }

    public static class Model {
        /** Immutable provider revision, deployment digest, or vendor snapshot ID. */
        private String revision = "unversioned";
        /**
         * 真正发给上游厂商的模型名，允许和业务侧请求的逻辑模型名不同。
         */
        @NotBlank
        private String upstreamModel;
        /**
         * 输入 token 单价，单位是每 1K token。
         */
        private BigDecimal inputPricePer1k = BigDecimal.ZERO;
        /**
         * 输出 token 单价，单位是每 1K token。
         */
        private BigDecimal outputPricePer1k = BigDecimal.ZERO;
        /** 厂商缓存命中输入 token 单价；未配置时回退到普通输入单价。 */
        private BigDecimal cachedInputPricePer1k;
        /** Prompt cache 创建/写入单价；未配置时按普通输入单价处理。 */
        private BigDecimal cacheWritePricePer1k;
        /** 价格币种。不同币种不能直接在总报表中相加。 */
        private String currency = "USD";
        /** 可审计的价格版本，例如 2026-07-14。 */
        private String priceVersion = "unversioned";
        /** 官方价格页，便于后续定时核验和人工审计。 */
        private String priceSource;
        /** 按单请求输入 token 数选择的阶梯价格，按 maxInputTokens 升序配置。 */
        private List<PriceTier> priceTiers = new ArrayList<>();

        /**
         * 读取当前配置或运行状态字段 get upstream model 的值，供调用方进行受控决策。
        */
        public String getUpstreamModel() {
            return upstreamModel;
        }

        /**
         * 更新配置字段 set upstream model；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setUpstreamModel(String upstreamModel) {
            this.upstreamModel = upstreamModel;
        }

        /**
         * 读取当前配置或运行状态字段 get revision 的值，供调用方进行受控决策。
        */
        public String getRevision() { return revision; }
        /**
         * 更新配置字段 set revision；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setRevision(String revision) { this.revision = revision; }

        /**
         * 读取当前配置或运行状态字段 get input price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getInputPricePer1k() {
            return inputPricePer1k;
        }

        /**
         * 更新配置字段 set input price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setInputPricePer1k(BigDecimal inputPricePer1k) {
            this.inputPricePer1k = inputPricePer1k;
        }

        /**
         * 读取当前配置或运行状态字段 get output price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getOutputPricePer1k() {
            return outputPricePer1k;
        }

        /**
         * 更新配置字段 set output price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setOutputPricePer1k(BigDecimal outputPricePer1k) {
            this.outputPricePer1k = outputPricePer1k;
        }

        /**
         * 读取当前配置或运行状态字段 get cached input price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getCachedInputPricePer1k() {
            return cachedInputPricePer1k;
        }

        /**
         * 更新配置字段 set cached input price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCachedInputPricePer1k(BigDecimal cachedInputPricePer1k) {
            this.cachedInputPricePer1k = cachedInputPricePer1k;
        }

        /**
         * 读取当前配置或运行状态字段 get cache write price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getCacheWritePricePer1k() { return cacheWritePricePer1k; }
        /**
         * 更新配置字段 set cache write price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCacheWritePricePer1k(BigDecimal cacheWritePricePer1k) { this.cacheWritePricePer1k = cacheWritePricePer1k; }

        /**
         * 读取当前配置或运行状态字段 get currency 的值，供调用方进行受控决策。
        */
        public String getCurrency() {
            return currency;
        }

        /**
         * 更新配置字段 set currency；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCurrency(String currency) {
            this.currency = currency;
        }

        /**
         * 读取当前配置或运行状态字段 get price version 的值，供调用方进行受控决策。
        */
        public String getPriceVersion() {
            return priceVersion;
        }

        /**
         * 更新配置字段 set price version；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPriceVersion(String priceVersion) {
            this.priceVersion = priceVersion;
        }

        /**
         * 读取当前配置或运行状态字段 get price source 的值，供调用方进行受控决策。
        */
        public String getPriceSource() {
            return priceSource;
        }

        /**
         * 更新配置字段 set price source；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPriceSource(String priceSource) {
            this.priceSource = priceSource;
        }

        /**
         * 读取当前配置或运行状态字段 get price tiers 的值，供调用方进行受控决策。
        */
        public List<PriceTier> getPriceTiers() { return priceTiers; }
        /**
         * 更新配置字段 set price tiers；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPriceTiers(List<PriceTier> priceTiers) { this.priceTiers = priceTiers; }
    }

    public static class PriceTier {
        private long maxInputTokens = Long.MAX_VALUE;
        private BigDecimal inputPricePer1k;
        private BigDecimal cachedInputPricePer1k;
        private BigDecimal cacheWritePricePer1k;
        private BigDecimal outputPricePer1k;

        /**
         * 读取当前配置或运行状态字段 get max input tokens 的值，供调用方进行受控决策。
        */
        public long getMaxInputTokens() { return maxInputTokens; }
        /**
         * 更新配置字段 set max input tokens；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setMaxInputTokens(long maxInputTokens) { this.maxInputTokens = maxInputTokens; }
        /**
         * 读取当前配置或运行状态字段 get input price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getInputPricePer1k() { return inputPricePer1k; }
        /**
         * 更新配置字段 set input price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setInputPricePer1k(BigDecimal inputPricePer1k) { this.inputPricePer1k = inputPricePer1k; }
        /**
         * 读取当前配置或运行状态字段 get cached input price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getCachedInputPricePer1k() { return cachedInputPricePer1k; }
        /**
         * 更新配置字段 set cached input price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCachedInputPricePer1k(BigDecimal cachedInputPricePer1k) { this.cachedInputPricePer1k = cachedInputPricePer1k; }
        /**
         * 读取当前配置或运行状态字段 get cache write price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getCacheWritePricePer1k() { return cacheWritePricePer1k; }
        /**
         * 更新配置字段 set cache write price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCacheWritePricePer1k(BigDecimal cacheWritePricePer1k) { this.cacheWritePricePer1k = cacheWritePricePer1k; }
        /**
         * 读取当前配置或运行状态字段 get output price per1k 的值，供调用方进行受控决策。
        */
        public BigDecimal getOutputPricePer1k() { return outputPricePer1k; }
        /**
         * 更新配置字段 set output price per1k；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setOutputPricePer1k(BigDecimal outputPricePer1k) { this.outputPricePer1k = outputPricePer1k; }
    }

    /**
     * 在线分位数回归配置。冷启动值只在样本不足时使用；有真实 usage 后模型会持续更新。
     */
    public static class CostPrediction {
        private boolean enabled = true;
        private double reservationQuantile = 0.95;
        private double learningRate = 2.0;
        private int conformalWindow = 1000;
        private int minSamples = 30;
        private long coldStartP50 = 512;
        private long coldStartP90 = 1536;
        private long coldStartP95 = 2048;
        private long coldStartP99 = 4096;

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() { return enabled; }
        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
        /**
         * 读取当前配置或运行状态字段 get reservation quantile 的值，供调用方进行受控决策。
        */
        public double getReservationQuantile() { return reservationQuantile; }
        /**
         * 更新配置字段 set reservation quantile；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setReservationQuantile(double reservationQuantile) { this.reservationQuantile = reservationQuantile; }
        /**
         * 读取当前配置或运行状态字段 get learning rate 的值，供调用方进行受控决策。
        */
        public double getLearningRate() { return learningRate; }
        /**
         * 更新配置字段 set learning rate；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setLearningRate(double learningRate) { this.learningRate = learningRate; }
        /**
         * 读取当前配置或运行状态字段 get conformal window 的值，供调用方进行受控决策。
        */
        public int getConformalWindow() { return conformalWindow; }
        /**
         * 更新配置字段 set conformal window；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setConformalWindow(int conformalWindow) { this.conformalWindow = conformalWindow; }
        /**
         * 读取当前配置或运行状态字段 get min samples 的值，供调用方进行受控决策。
        */
        public int getMinSamples() { return minSamples; }
        /**
         * 更新配置字段 set min samples；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setMinSamples(int minSamples) { this.minSamples = minSamples; }
        /**
         * 读取当前配置或运行状态字段 get cold start p50 的值，供调用方进行受控决策。
        */
        public long getColdStartP50() { return coldStartP50; }
        /**
         * 更新配置字段 set cold start p50；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setColdStartP50(long coldStartP50) { this.coldStartP50 = coldStartP50; }
        /**
         * 读取当前配置或运行状态字段 get cold start p90 的值，供调用方进行受控决策。
        */
        public long getColdStartP90() { return coldStartP90; }
        /**
         * 更新配置字段 set cold start p90；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setColdStartP90(long coldStartP90) { this.coldStartP90 = coldStartP90; }
        /**
         * 读取当前配置或运行状态字段 get cold start p95 的值，供调用方进行受控决策。
        */
        public long getColdStartP95() { return coldStartP95; }
        /**
         * 更新配置字段 set cold start p95；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setColdStartP95(long coldStartP95) { this.coldStartP95 = coldStartP95; }
        /**
         * 读取当前配置或运行状态字段 get cold start p99 的值，供调用方进行受控决策。
        */
        public long getColdStartP99() { return coldStartP99; }
        /**
         * 更新配置字段 set cold start p99；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setColdStartP99(long coldStartP99) { this.coldStartP99 = coldStartP99; }
    }

    public static class Billing {
        private String baseCurrency = "CNY";
        private String exchangeRateVersion = "2026-07-manual";
        private Map<String, BigDecimal> exchangeRates = new LinkedHashMap<>(Map.of(
                "CNY", BigDecimal.ONE,
                "USD", new BigDecimal("7.20")
        ));

        /**
         * 读取当前配置或运行状态字段 get base currency 的值，供调用方进行受控决策。
        */
        public String getBaseCurrency() { return baseCurrency; }
        /**
         * 更新配置字段 set base currency；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setBaseCurrency(String baseCurrency) { this.baseCurrency = baseCurrency; }
        /**
         * 读取当前配置或运行状态字段 get exchange rate version 的值，供调用方进行受控决策。
        */
        public String getExchangeRateVersion() { return exchangeRateVersion; }
        /**
         * 更新配置字段 set exchange rate version；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setExchangeRateVersion(String exchangeRateVersion) { this.exchangeRateVersion = exchangeRateVersion; }
        /**
         * 读取当前配置或运行状态字段 get exchange rates 的值，供调用方进行受控决策。
        */
        public Map<String, BigDecimal> getExchangeRates() { return exchangeRates; }
        /**
         * 更新配置字段 set exchange rates；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setExchangeRates(Map<String, BigDecimal> exchangeRates) { this.exchangeRates = exchangeRates; }
    }

    public static class Route {
        /** Control Plane release version for this logical route. */
        private String version = "unversioned";
        /**
         * 默认优先调用的目标，格式固定为 provider:model。
         */
        @NotBlank
        /*
         * 默认主模型，格式固定为 provider:model，例如 deepseek:deepseek-v4-flash。
         *
         * primary 的定位是“稳定主链路”：它通常选择效果、成本、延迟都经过验证的模型。
         * 当没有命中 canary 灰度路由、也没有配置 weighted 权重路由时，网关就调用 primary。
         */
        private String primary;
        /**
         * primary 失败后的降级链路，按顺序尝试。
         */
        /*
         * 降级链路，格式同样是 provider:model，按列表顺序依次尝试。
         *
         * fallbacks 的定位是“故障兜底”：当 primary/weighted/canary 选出的模型超时、
         * 限流、熔断、网络失败或厂商返回异常时，ChatGatewayService 会继续调用这里的下一个模型。
         */
        private List<String> fallbacks = new ArrayList<>();
        /**
         * 权重路由目标，用于负载均衡或模型成本优化。
         */
        /*
         * 权重路由目标，适合多模型分摊流量、成本优化和 A/B 对比。
         *
         * weighted 和 primary 的区别：
         * - primary 是固定默认目标；
         * - weighted 是在多个目标之间按 weight 随机抽样，例如 80% 走低成本模型、20% 走高质量模型。
         */
        private List<WeightedTarget> weighted = new ArrayList<>();
        /**
         * 灰度路由目标，用于小比例验证新模型或新供应商。
         */
        /*
         * 灰度路由目标，适合把少量真实流量打到新模型、新供应商或新参数配置。
         *
         * canary 和 weighted 的区别：
         * - canary 关注“新方案能不能上线”，通常是 1%、5%、10% 这种小比例验证；
         * - weighted 关注“多个成熟方案如何长期分配流量”，可以长期保留。
         */
        private List<CanaryTarget> canary = new ArrayList<>();

        /**
         * 读取当前配置或运行状态字段 get primary 的值，供调用方进行受控决策。
        */
        public String getPrimary() {
            return primary;
        }

        /**
         * 更新配置字段 set primary；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPrimary(String primary) {
            this.primary = primary;
        }

        /**
         * 读取当前配置或运行状态字段 get version 的值，供调用方进行受控决策。
        */
        public String getVersion() { return version; }
        /**
         * 更新配置字段 set version；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setVersion(String version) { this.version = version; }

        /**
         * 读取当前配置或运行状态字段 get fallbacks 的值，供调用方进行受控决策。
        */
        public List<String> getFallbacks() {
            return fallbacks;
        }

        /**
         * 更新配置字段 set fallbacks；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setFallbacks(List<String> fallbacks) {
            this.fallbacks = fallbacks;
        }

        /**
         * 读取当前配置或运行状态字段 get weighted 的值，供调用方进行受控决策。
        */
        public List<WeightedTarget> getWeighted() {
            return weighted;
        }

        /**
         * 更新配置字段 set weighted；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setWeighted(List<WeightedTarget> weighted) {
            this.weighted = weighted;
        }

        /**
         * 读取当前配置或运行状态字段 get canary 的值，供调用方进行受控决策。
        */
        public List<CanaryTarget> getCanary() {
            return canary;
        }

        /**
         * 更新配置字段 set canary；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCanary(List<CanaryTarget> canary) {
            this.canary = canary;
        }
    }

    public static class WeightedTarget {
        @NotBlank
        /*
         * 权重路由的实际目标，格式为 provider:model。
         */
        private String target;
        /*
         * 抽样权重，不是百分比。
         *
         * 例如 A.weight=8、B.weight=2，表示 A 理论命中概率约 80%，B 约 20%。
         */
        private int weight = 1;

        /**
         * 读取当前配置或运行状态字段 get target 的值，供调用方进行受控决策。
        */
        public String getTarget() {
            return target;
        }

        /**
         * 更新配置字段 set target；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTarget(String target) {
            this.target = target;
        }

        /**
         * 读取当前配置或运行状态字段 get weight 的值，供调用方进行受控决策。
        */
        public int getWeight() {
            return weight;
        }

        /**
         * 更新配置字段 set weight；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setWeight(int weight) {
            this.weight = weight;
        }
    }

    public static class CanaryTarget {
        @NotBlank
        /*
         * 灰度路由的实际目标，格式为 provider:model。
         */
        private String target;
        /*
         * 灰度百分比，取值范围在 ModelRouter 中会被截断到 0-100。
         *
         * percent=5 表示大约 5% 的请求进入该灰度目标；未命中灰度时再进入 weighted 或 primary。
         */
        private int percent = 0;

        /**
         * 读取当前配置或运行状态字段 get target 的值，供调用方进行受控决策。
        */
        public String getTarget() {
            return target;
        }

        /**
         * 更新配置字段 set target；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTarget(String target) {
            this.target = target;
        }

        /**
         * 读取当前配置或运行状态字段 get percent 的值，供调用方进行受控决策。
        */
        public int getPercent() {
            return percent;
        }

        /**
         * 更新配置字段 set percent；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPercent(int percent) {
            this.percent = percent;
        }
    }

    public static class UserQuota {
        private long dailyTokenLimit = 50_000;
        private BigDecimal dailyCostLimit = BigDecimal.ONE;

        /**
         * 读取当前配置或运行状态字段 get daily token limit 的值，供调用方进行受控决策。
        */
        public long getDailyTokenLimit() {
            return dailyTokenLimit;
        }

        /**
         * 更新配置字段 set daily token limit；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setDailyTokenLimit(long dailyTokenLimit) {
            this.dailyTokenLimit = dailyTokenLimit;
        }

        /**
         * 读取当前配置或运行状态字段 get daily cost limit 的值，供调用方进行受控决策。
        */
        public BigDecimal getDailyCostLimit() {
            return dailyCostLimit;
        }

        /**
         * 更新配置字段 set daily cost limit；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setDailyCostLimit(BigDecimal dailyCostLimit) {
            this.dailyCostLimit = dailyCostLimit;
        }
    }

    public static class ApiKey {
        private boolean enabled = true;
        /**
         * 租户 ID 用于隔离缓存、报表和后续的预算策略。
         */
        private String tenantId = "default";
        private String userId = "anonymous";
        /** Trusted internal callers may carry the end-user tenant in X-Tenant-Id. */
        private boolean trustedService = false;
        /**
         * 为空表示不限制模型；非空时只允许访问列表中的逻辑模型名。
         */
        private List<String> allowedModels = new ArrayList<>();

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        /**
         * 读取当前配置或运行状态字段 get tenant id 的值，供调用方进行受控决策。
        */
        public String getTenantId() {
            return tenantId;
        }

        /**
         * 更新配置字段 set tenant id；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTenantId(String tenantId) {
            this.tenantId = tenantId;
        }

        /**
         * 读取当前配置或运行状态字段 get user id 的值，供调用方进行受控决策。
        */
        public String getUserId() {
            return userId;
        }

        /**
         * 更新配置字段 set user id；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setUserId(String userId) {
            this.userId = userId;
        }

        /**
         * 读取当前配置或运行状态字段 is trusted service 的值，供调用方进行受控决策。
        */
        public boolean isTrustedService() {
            return trustedService;
        }

        /**
         * 更新配置字段 set trusted service；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTrustedService(boolean trustedService) {
            this.trustedService = trustedService;
        }

        /**
         * 读取当前配置或运行状态字段 get allowed models 的值，供调用方进行受控决策。
        */
        public List<String> getAllowedModels() {
            return allowedModels;
        }

        /**
         * 更新配置字段 set allowed models；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setAllowedModels(List<String> allowedModels) {
            this.allowedModels = allowedModels;
        }
    }

    public static class Cache {
        private boolean enabled = true;
        private int maxEntries = 1000;
        private Duration ttl = Duration.ofMinutes(10);
        private Duration randomTtlJitter = Duration.ofSeconds(30);
        private boolean requireExplicitOptIn = true;
        private boolean mutexProtectionEnabled = true;

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        /**
         * 读取当前配置或运行状态字段 get max entries 的值，供调用方进行受控决策。
        */
        public int getMaxEntries() {
            return maxEntries;
        }

        /**
         * 更新配置字段 set max entries；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setMaxEntries(int maxEntries) {
            this.maxEntries = maxEntries;
        }

        /**
         * 读取当前配置或运行状态字段 get ttl 的值，供调用方进行受控决策。
        */
        public Duration getTtl() {
            return ttl;
        }

        /**
         * 更新配置字段 set ttl；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTtl(Duration ttl) {
            this.ttl = ttl;
        }

        /**
         * 读取当前配置或运行状态字段 get random ttl jitter 的值，供调用方进行受控决策。
        */
        public Duration getRandomTtlJitter() {
            return randomTtlJitter;
        }

        /**
         * 更新配置字段 set random ttl jitter；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setRandomTtlJitter(Duration randomTtlJitter) {
            this.randomTtlJitter = randomTtlJitter;
        }

        /**
         * 读取当前配置或运行状态字段 is mutex protection enabled 的值，供调用方进行受控决策。
        */
        public boolean isMutexProtectionEnabled() {
            return mutexProtectionEnabled;
        }

        /**
         * 更新配置字段 set mutex protection enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setMutexProtectionEnabled(boolean mutexProtectionEnabled) {
            this.mutexProtectionEnabled = mutexProtectionEnabled;
        }

        /**
         * 读取当前配置或运行状态字段 is require explicit opt in 的值，供调用方进行受控决策。
        */
        public boolean isRequireExplicitOptIn() {
            return requireExplicitOptIn;
        }

        /**
         * 更新配置字段 set require explicit opt in；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setRequireExplicitOptIn(boolean requireExplicitOptIn) {
            this.requireExplicitOptIn = requireExplicitOptIn;
        }
    }

    public static class Resilience {
        private int circuitFailureThreshold = 3;
        private Duration circuitOpenDuration = Duration.ofSeconds(30);

        /**
         * 读取当前配置或运行状态字段 get circuit failure threshold 的值，供调用方进行受控决策。
        */
        public int getCircuitFailureThreshold() {
            return circuitFailureThreshold;
        }

        /**
         * 更新配置字段 set circuit failure threshold；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCircuitFailureThreshold(int circuitFailureThreshold) {
            this.circuitFailureThreshold = circuitFailureThreshold;
        }

        /**
         * 读取当前配置或运行状态字段 get circuit open duration 的值，供调用方进行受控决策。
        */
        public Duration getCircuitOpenDuration() {
            return circuitOpenDuration;
        }

        /**
         * 更新配置字段 set circuit open duration；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setCircuitOpenDuration(Duration circuitOpenDuration) {
            this.circuitOpenDuration = circuitOpenDuration;
        }

    }

    /**
     * 多维实时准入配置。
     *
     * <p>值为零表示该维度不限制。请求和 Token 以分钟为窗口；并发许可必须在响应终止时归还。</p>
     */
    public static class Admission {
        private String store = "memory";
        private long tenantRequestsPerMinute = 600;
        private long userRequestsPerMinute = 120;
        private long routeRequestsPerMinute = 120;
        private long providerRequestsPerMinute = 600;
        private long tenantTokensPerMinute = 500_000;
        private long userTokensPerMinute = 200_000;
        private long routeTokensPerMinute = 500_000;
        private long providerTokensPerMinute = 1_000_000;
        private long tenantMaxConcurrency = 100;
        private long userMaxConcurrency = 20;
        private long routeMaxConcurrency = 50;
        private long providerMaxConcurrency = 200;
        private int rateBurstSeconds = 60;
        private int concurrencyLeaseTtlSeconds = 240;
        private int maxRequestBytes = 1_048_576;
        private int maxMessages = 128;
        private long maxPromptTokens = 128_000;
        private long maxCompletionTokens = 16_384;
        private int maxUpstreamAttempts = 3;

        /** 返回准入状态存储类型；生产应为 redis。 */ public String getStore() { return store; }
        /** 绑定准入状态存储类型。 */ public void setStore(String store) { this.store = store; }
        /** 返回租户每分钟请求上限。 */ public long getTenantRequestsPerMinute() { return tenantRequestsPerMinute; }
        /** 绑定租户每分钟请求上限。 */ public void setTenantRequestsPerMinute(long value) { tenantRequestsPerMinute = value; }
        /** 返回用户每分钟请求上限。 */ public long getUserRequestsPerMinute() { return userRequestsPerMinute; }
        /** 绑定用户每分钟请求上限。 */ public void setUserRequestsPerMinute(long value) { userRequestsPerMinute = value; }
        /** 返回路由每分钟请求上限。 */ public long getRouteRequestsPerMinute() { return routeRequestsPerMinute; }
        /** 绑定路由每分钟请求上限。 */ public void setRouteRequestsPerMinute(long value) { routeRequestsPerMinute = value; }
        /** 返回供应商每分钟请求上限。 */ public long getProviderRequestsPerMinute() { return providerRequestsPerMinute; }
        /** 绑定供应商每分钟请求上限。 */ public void setProviderRequestsPerMinute(long value) { providerRequestsPerMinute = value; }
        /** 返回租户每分钟 Token 上限。 */ public long getTenantTokensPerMinute() { return tenantTokensPerMinute; }
        /** 绑定租户每分钟 Token 上限。 */ public void setTenantTokensPerMinute(long value) { tenantTokensPerMinute = value; }
        /** 返回用户每分钟 Token 上限。 */ public long getUserTokensPerMinute() { return userTokensPerMinute; }
        /** 绑定用户每分钟 Token 上限。 */ public void setUserTokensPerMinute(long value) { userTokensPerMinute = value; }
        /** 返回路由每分钟 Token 上限。 */ public long getRouteTokensPerMinute() { return routeTokensPerMinute; }
        /** 绑定路由每分钟 Token 上限。 */ public void setRouteTokensPerMinute(long value) { routeTokensPerMinute = value; }
        /** 返回供应商每分钟 Token 上限。 */ public long getProviderTokensPerMinute() { return providerTokensPerMinute; }
        /** 绑定供应商每分钟 Token 上限。 */ public void setProviderTokensPerMinute(long value) { providerTokensPerMinute = value; }
        /** 返回租户最大在途请求数。 */ public long getTenantMaxConcurrency() { return tenantMaxConcurrency; }
        /** 绑定租户最大在途请求数。 */ public void setTenantMaxConcurrency(long value) { tenantMaxConcurrency = value; }
        /** 返回用户最大在途请求数。 */ public long getUserMaxConcurrency() { return userMaxConcurrency; }
        /** 绑定用户最大在途请求数。 */ public void setUserMaxConcurrency(long value) { userMaxConcurrency = value; }
        /** 返回路由最大在途请求数。 */ public long getRouteMaxConcurrency() { return routeMaxConcurrency; }
        /** 绑定路由最大在途请求数。 */ public void setRouteMaxConcurrency(long value) { routeMaxConcurrency = value; }
        /** 返回供应商最大在途请求数。 */ public long getProviderMaxConcurrency() { return providerMaxConcurrency; }
        /** 绑定供应商最大在途请求数。 */ public void setProviderMaxConcurrency(long value) { providerMaxConcurrency = value; }
        /** 返回令牌桶允许的最大突发时间窗口。 */ public int getRateBurstSeconds() { return rateBurstSeconds; }
        /** 绑定令牌桶允许的最大突发时间窗口。 */ public void setRateBurstSeconds(int value) { rateBurstSeconds = value; }
        /** 返回并发许可的兜底过期秒数，防止进程崩溃永久占用。 */ public int getConcurrencyLeaseTtlSeconds() { return concurrencyLeaseTtlSeconds; }
        /** 绑定并发许可的兜底过期秒数。 */ public void setConcurrencyLeaseTtlSeconds(int value) { concurrencyLeaseTtlSeconds = value; }
        /** 返回允许的最大 JSON 字节数。 */ public int getMaxRequestBytes() { return maxRequestBytes; }
        /** 绑定允许的最大 JSON 字节数。 */ public void setMaxRequestBytes(int value) { maxRequestBytes = value; }
        /** 返回一条请求允许的最大消息数。 */ public int getMaxMessages() { return maxMessages; }
        /** 绑定一条请求允许的最大消息数。 */ public void setMaxMessages(int value) { maxMessages = value; }
        /** 返回允许的最大输入 Token。 */ public long getMaxPromptTokens() { return maxPromptTokens; }
        /** 绑定允许的最大输入 Token。 */ public void setMaxPromptTokens(long value) { maxPromptTokens = value; }
        /** 返回允许的最大输出 Token。 */ public long getMaxCompletionTokens() { return maxCompletionTokens; }
        /** 绑定允许的最大输出 Token。 */ public void setMaxCompletionTokens(long value) { maxCompletionTokens = value; }
        /** 返回单个客户端请求允许的最大真实上游尝试数，覆盖 fallback 链。 */ public int getMaxUpstreamAttempts() { return maxUpstreamAttempts; }
        /** 绑定单个客户端请求允许的最大真实上游尝试数。 */ public void setMaxUpstreamAttempts(int value) { maxUpstreamAttempts = value; }
    }

    public static class Persistence {
        private boolean enabled = true;

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class Admin {
        private Security security = new Security();

        /**
         * 读取当前配置或运行状态字段 get security 的值，供调用方进行受控决策。
        */
        public Security getSecurity() {
            return security;
        }

        /**
         * 更新配置字段 set security；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setSecurity(Security security) {
            this.security = security;
        }
    }

    public static class Compatibility {
        private AgentMemory agentMemory = new AgentMemory();
        private ComplianceWorkflow complianceWorkflow = new ComplianceWorkflow();

        /**
         * 读取当前配置或运行状态字段 get agent memory 的值，供调用方进行受控决策。
        */
        public AgentMemory getAgentMemory() {
            return agentMemory;
        }

        /**
         * 更新配置字段 set agent memory；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setAgentMemory(AgentMemory agentMemory) {
            this.agentMemory = agentMemory;
        }

        /**
         * 读取当前配置或运行状态字段 get compliance workflow 的值，供调用方进行受控决策。
        */
        public ComplianceWorkflow getComplianceWorkflow() {
            return complianceWorkflow;
        }

        /**
         * 更新配置字段 set compliance workflow；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setComplianceWorkflow(ComplianceWorkflow complianceWorkflow) {
            this.complianceWorkflow = complianceWorkflow;
        }
    }

    public static class AgentMemory {
        private boolean enabled;

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class ComplianceWorkflow {
        private boolean enabled;

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class Security {
        private boolean enabled = true;
        private String username = "admin";
        private String password = "admin123";

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        /**
         * 读取当前配置或运行状态字段 get username 的值，供调用方进行受控决策。
        */
        public String getUsername() {
            return username;
        }

        /**
         * 更新配置字段 set username；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setUsername(String username) {
            this.username = username;
        }

        /**
         * 读取当前配置或运行状态字段 get password 的值，供调用方进行受控决策。
        */
        public String getPassword() {
            return password;
        }

        /**
         * 更新配置字段 set password；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setPassword(String password) {
            this.password = password;
        }
    }

    public static class Oidc {
        private boolean enabled;
        private String tenantClaim = "tenant_id";
        private String rolesClaim = "roles";

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        /**
         * 读取当前配置或运行状态字段 get tenant claim 的值，供调用方进行受控决策。
        */
        public String getTenantClaim() {
            return tenantClaim;
        }

        /**
         * 更新配置字段 set tenant claim；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setTenantClaim(String tenantClaim) {
            this.tenantClaim = tenantClaim;
        }

        /**
         * 读取当前配置或运行状态字段 get roles claim 的值，供调用方进行受控决策。
        */
        public String getRolesClaim() {
            return rolesClaim;
        }

        /**
         * 更新配置字段 set roles claim；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setRolesClaim(String rolesClaim) {
            this.rolesClaim = rolesClaim;
        }
    }

    public static class Opa {
        private boolean enabled;
        private String baseUrl = "http://localhost:8181";
        private String decisionPath = "agent_platform/llm/allow";

        /**
         * 读取当前配置或运行状态字段 is enabled 的值，供调用方进行受控决策。
        */
        public boolean isEnabled() {
            return enabled;
        }

        /**
         * 更新配置字段 set enabled；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        /**
         * 读取当前配置或运行状态字段 get base url 的值，供调用方进行受控决策。
        */
        public String getBaseUrl() {
            return baseUrl;
        }

        /**
         * 更新配置字段 set base url；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setBaseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
        }

        /**
         * 读取当前配置或运行状态字段 get decision path 的值，供调用方进行受控决策。
        */
        public String getDecisionPath() {
            return decisionPath;
        }

        /**
         * 更新配置字段 set decision path；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setDecisionPath(String decisionPath) {
            this.decisionPath = decisionPath;
        }
    }

    public static class PromptTemplate {
        /**
         * 追加到 messages 开头的 system prompt。
         */
        /*
         * system 模板会被追加到 messages 开头，负责稳定约束模型行为。
         *
         * 常见内容包括：
         * - 模型扮演什么角色；
         * - 必须遵守哪些安全边界；
         * - 输出必须是 Markdown、JSON 还是固定字段；
         * - 遇到高风险或不确定输入时如何兜底。
         *
         * 它一般不放具体业务变量，具体变量由 variables 渲染到 user 模板里。
         */
        private String system;
        /**
         * 可选用户模板，适合把业务变量拼装成固定问法。
         */
        /*
         * user 模板负责描述本次具体业务任务，通常包含 {{变量名}} 占位符。
         *
         * 例如：
         * “请围绕 {{topic}}，结合 {{project}}，用面试回答格式输出。”
         *
         * 请求体中的 variables 会提供 topic、project 等实际值，PromptTemplateService
         * 会把这些占位符替换成真实业务数据，再发给上游模型。
         */
        private String user;

        /**
         * 读取当前配置或运行状态字段 get system 的值，供调用方进行受控决策。
        */
        public String getSystem() {
            return system;
        }

        /**
         * 更新配置字段 set system；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setSystem(String system) {
            this.system = system;
        }

        /**
         * 读取当前配置或运行状态字段 get user 的值，供调用方进行受控决策。
        */
        public String getUser() {
            return user;
        }

        /**
         * 更新配置字段 set user；该值由 Spring 配置绑定或受控管理接口提供。
        */
        public void setUser(String user) {
            this.user = user;
        }
    }
}
