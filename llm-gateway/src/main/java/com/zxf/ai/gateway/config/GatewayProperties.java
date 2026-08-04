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

    public Oidc getOidc() {
        return oidc;
    }

    public void setOidc(Oidc oidc) {
        this.oidc = oidc;
    }

    public Opa getOpa() {
        return opa;
    }

    public void setOpa(Opa opa) {
        this.opa = opa;
    }

    public Duration getRequestTimeout() {
        return requestTimeout;
    }

    public void setRequestTimeout(Duration requestTimeout) {
        this.requestTimeout = requestTimeout;
    }

    public int getMaxRetries() {
        return maxRetries;
    }

    public void setMaxRetries(int maxRetries) {
        this.maxRetries = maxRetries;
    }

    public String getQuotaStore() {
        return quotaStore;
    }

    public void setQuotaStore(String quotaStore) {
        this.quotaStore = quotaStore;
    }

    public boolean isAllowAnonymous() {
        return allowAnonymous;
    }

    public void setAllowAnonymous(boolean allowAnonymous) {
        this.allowAnonymous = allowAnonymous;
    }

    public Compatibility getCompatibility() {
        return compatibility;
    }

    public void setCompatibility(Compatibility compatibility) {
        this.compatibility = compatibility;
    }

    public String getDefaultModel() {
        return defaultModel;
    }

    public void setDefaultModel(String defaultModel) {
        this.defaultModel = defaultModel;
    }

    public Map<String, Provider> getProviders() {
        return providers;
    }

    public void setProviders(Map<String, Provider> providers) {
        this.providers = providers;
    }

    public Map<String, Route> getRoutes() {
        return routes;
    }

    public void setRoutes(Map<String, Route> routes) {
        this.routes = routes;
    }

    public Map<String, UserQuota> getUserQuotas() {
        return userQuotas;
    }

    public void setUserQuotas(Map<String, UserQuota> userQuotas) {
        this.userQuotas = userQuotas;
    }

    public Map<String, ApiKey> getApiKeys() {
        return apiKeys;
    }

    public void setApiKeys(Map<String, ApiKey> apiKeys) {
        this.apiKeys = apiKeys;
    }

    public Cache getCache() {
        return cache;
    }

    public void setCache(Cache cache) {
        this.cache = cache;
    }

    public Resilience getResilience() {
        return resilience;
    }

    public void setResilience(Resilience resilience) {
        this.resilience = resilience;
    }

    public Persistence getPersistence() {
        return persistence;
    }

    public void setPersistence(Persistence persistence) {
        this.persistence = persistence;
    }

    public Admin getAdmin() {
        return admin;
    }

    public void setAdmin(Admin admin) {
        this.admin = admin;
    }

    public CostPrediction getCostPrediction() {
        return costPrediction;
    }

    public void setCostPrediction(CostPrediction costPrediction) {
        this.costPrediction = costPrediction;
    }

    public Billing getBilling() { return billing; }
    public void setBilling(Billing billing) { this.billing = billing; }

    public Map<String, PromptTemplate> getPromptTemplates() {
        return promptTemplates;
    }

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

        public String getBaseUrl() {
            return baseUrl;
        }

        public void setBaseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
        }

        public String getProtocol() {
            return protocol;
        }

        public void setProtocol(String protocol) {
            this.protocol = protocol;
        }

        public String getApiKey() {
            return apiKey;
        }

        public void setApiKey(String apiKey) {
            this.apiKey = apiKey;
        }

        public Map<String, Model> getModels() {
            return models;
        }

        public void setModels(Map<String, Model> models) {
            this.models = models;
        }
    }

    public static class Model {
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

        public String getUpstreamModel() {
            return upstreamModel;
        }

        public void setUpstreamModel(String upstreamModel) {
            this.upstreamModel = upstreamModel;
        }

        public BigDecimal getInputPricePer1k() {
            return inputPricePer1k;
        }

        public void setInputPricePer1k(BigDecimal inputPricePer1k) {
            this.inputPricePer1k = inputPricePer1k;
        }

        public BigDecimal getOutputPricePer1k() {
            return outputPricePer1k;
        }

        public void setOutputPricePer1k(BigDecimal outputPricePer1k) {
            this.outputPricePer1k = outputPricePer1k;
        }

        public BigDecimal getCachedInputPricePer1k() {
            return cachedInputPricePer1k;
        }

        public void setCachedInputPricePer1k(BigDecimal cachedInputPricePer1k) {
            this.cachedInputPricePer1k = cachedInputPricePer1k;
        }

        public BigDecimal getCacheWritePricePer1k() { return cacheWritePricePer1k; }
        public void setCacheWritePricePer1k(BigDecimal cacheWritePricePer1k) { this.cacheWritePricePer1k = cacheWritePricePer1k; }

        public String getCurrency() {
            return currency;
        }

        public void setCurrency(String currency) {
            this.currency = currency;
        }

        public String getPriceVersion() {
            return priceVersion;
        }

        public void setPriceVersion(String priceVersion) {
            this.priceVersion = priceVersion;
        }

        public String getPriceSource() {
            return priceSource;
        }

        public void setPriceSource(String priceSource) {
            this.priceSource = priceSource;
        }

        public List<PriceTier> getPriceTiers() { return priceTiers; }
        public void setPriceTiers(List<PriceTier> priceTiers) { this.priceTiers = priceTiers; }
    }

    public static class PriceTier {
        private long maxInputTokens = Long.MAX_VALUE;
        private BigDecimal inputPricePer1k;
        private BigDecimal cachedInputPricePer1k;
        private BigDecimal cacheWritePricePer1k;
        private BigDecimal outputPricePer1k;

        public long getMaxInputTokens() { return maxInputTokens; }
        public void setMaxInputTokens(long maxInputTokens) { this.maxInputTokens = maxInputTokens; }
        public BigDecimal getInputPricePer1k() { return inputPricePer1k; }
        public void setInputPricePer1k(BigDecimal inputPricePer1k) { this.inputPricePer1k = inputPricePer1k; }
        public BigDecimal getCachedInputPricePer1k() { return cachedInputPricePer1k; }
        public void setCachedInputPricePer1k(BigDecimal cachedInputPricePer1k) { this.cachedInputPricePer1k = cachedInputPricePer1k; }
        public BigDecimal getCacheWritePricePer1k() { return cacheWritePricePer1k; }
        public void setCacheWritePricePer1k(BigDecimal cacheWritePricePer1k) { this.cacheWritePricePer1k = cacheWritePricePer1k; }
        public BigDecimal getOutputPricePer1k() { return outputPricePer1k; }
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

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
        public double getReservationQuantile() { return reservationQuantile; }
        public void setReservationQuantile(double reservationQuantile) { this.reservationQuantile = reservationQuantile; }
        public double getLearningRate() { return learningRate; }
        public void setLearningRate(double learningRate) { this.learningRate = learningRate; }
        public int getConformalWindow() { return conformalWindow; }
        public void setConformalWindow(int conformalWindow) { this.conformalWindow = conformalWindow; }
        public int getMinSamples() { return minSamples; }
        public void setMinSamples(int minSamples) { this.minSamples = minSamples; }
        public long getColdStartP50() { return coldStartP50; }
        public void setColdStartP50(long coldStartP50) { this.coldStartP50 = coldStartP50; }
        public long getColdStartP90() { return coldStartP90; }
        public void setColdStartP90(long coldStartP90) { this.coldStartP90 = coldStartP90; }
        public long getColdStartP95() { return coldStartP95; }
        public void setColdStartP95(long coldStartP95) { this.coldStartP95 = coldStartP95; }
        public long getColdStartP99() { return coldStartP99; }
        public void setColdStartP99(long coldStartP99) { this.coldStartP99 = coldStartP99; }
    }

    public static class Billing {
        private String baseCurrency = "CNY";
        private String exchangeRateVersion = "2026-07-manual";
        private Map<String, BigDecimal> exchangeRates = new LinkedHashMap<>(Map.of(
                "CNY", BigDecimal.ONE,
                "USD", new BigDecimal("7.20")
        ));

        public String getBaseCurrency() { return baseCurrency; }
        public void setBaseCurrency(String baseCurrency) { this.baseCurrency = baseCurrency; }
        public String getExchangeRateVersion() { return exchangeRateVersion; }
        public void setExchangeRateVersion(String exchangeRateVersion) { this.exchangeRateVersion = exchangeRateVersion; }
        public Map<String, BigDecimal> getExchangeRates() { return exchangeRates; }
        public void setExchangeRates(Map<String, BigDecimal> exchangeRates) { this.exchangeRates = exchangeRates; }
    }

    public static class Route {
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

        public String getPrimary() {
            return primary;
        }

        public void setPrimary(String primary) {
            this.primary = primary;
        }

        public List<String> getFallbacks() {
            return fallbacks;
        }

        public void setFallbacks(List<String> fallbacks) {
            this.fallbacks = fallbacks;
        }

        public List<WeightedTarget> getWeighted() {
            return weighted;
        }

        public void setWeighted(List<WeightedTarget> weighted) {
            this.weighted = weighted;
        }

        public List<CanaryTarget> getCanary() {
            return canary;
        }

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

        public String getTarget() {
            return target;
        }

        public void setTarget(String target) {
            this.target = target;
        }

        public int getWeight() {
            return weight;
        }

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

        public String getTarget() {
            return target;
        }

        public void setTarget(String target) {
            this.target = target;
        }

        public int getPercent() {
            return percent;
        }

        public void setPercent(int percent) {
            this.percent = percent;
        }
    }

    public static class UserQuota {
        private long dailyTokenLimit = 50_000;
        private BigDecimal dailyCostLimit = BigDecimal.ONE;

        public long getDailyTokenLimit() {
            return dailyTokenLimit;
        }

        public void setDailyTokenLimit(long dailyTokenLimit) {
            this.dailyTokenLimit = dailyTokenLimit;
        }

        public BigDecimal getDailyCostLimit() {
            return dailyCostLimit;
        }

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

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        public String getTenantId() {
            return tenantId;
        }

        public void setTenantId(String tenantId) {
            this.tenantId = tenantId;
        }

        public String getUserId() {
            return userId;
        }

        public void setUserId(String userId) {
            this.userId = userId;
        }

        public boolean isTrustedService() {
            return trustedService;
        }

        public void setTrustedService(boolean trustedService) {
            this.trustedService = trustedService;
        }

        public List<String> getAllowedModels() {
            return allowedModels;
        }

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

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        public int getMaxEntries() {
            return maxEntries;
        }

        public void setMaxEntries(int maxEntries) {
            this.maxEntries = maxEntries;
        }

        public Duration getTtl() {
            return ttl;
        }

        public void setTtl(Duration ttl) {
            this.ttl = ttl;
        }

        public Duration getRandomTtlJitter() {
            return randomTtlJitter;
        }

        public void setRandomTtlJitter(Duration randomTtlJitter) {
            this.randomTtlJitter = randomTtlJitter;
        }

        public boolean isMutexProtectionEnabled() {
            return mutexProtectionEnabled;
        }

        public void setMutexProtectionEnabled(boolean mutexProtectionEnabled) {
            this.mutexProtectionEnabled = mutexProtectionEnabled;
        }

        public boolean isRequireExplicitOptIn() {
            return requireExplicitOptIn;
        }

        public void setRequireExplicitOptIn(boolean requireExplicitOptIn) {
            this.requireExplicitOptIn = requireExplicitOptIn;
        }
    }

    public static class Resilience {
        private int circuitFailureThreshold = 3;
        private Duration circuitOpenDuration = Duration.ofSeconds(30);
        private int routeRateLimitPerMinute = 120;

        public int getCircuitFailureThreshold() {
            return circuitFailureThreshold;
        }

        public void setCircuitFailureThreshold(int circuitFailureThreshold) {
            this.circuitFailureThreshold = circuitFailureThreshold;
        }

        public Duration getCircuitOpenDuration() {
            return circuitOpenDuration;
        }

        public void setCircuitOpenDuration(Duration circuitOpenDuration) {
            this.circuitOpenDuration = circuitOpenDuration;
        }

        public int getRouteRateLimitPerMinute() {
            return routeRateLimitPerMinute;
        }

        public void setRouteRateLimitPerMinute(int routeRateLimitPerMinute) {
            this.routeRateLimitPerMinute = routeRateLimitPerMinute;
        }
    }

    public static class Persistence {
        private boolean enabled = true;

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class Admin {
        private Security security = new Security();

        public Security getSecurity() {
            return security;
        }

        public void setSecurity(Security security) {
            this.security = security;
        }
    }

    public static class Compatibility {
        private AgentMemory agentMemory = new AgentMemory();
        private ComplianceWorkflow complianceWorkflow = new ComplianceWorkflow();

        public AgentMemory getAgentMemory() {
            return agentMemory;
        }

        public void setAgentMemory(AgentMemory agentMemory) {
            this.agentMemory = agentMemory;
        }

        public ComplianceWorkflow getComplianceWorkflow() {
            return complianceWorkflow;
        }

        public void setComplianceWorkflow(ComplianceWorkflow complianceWorkflow) {
            this.complianceWorkflow = complianceWorkflow;
        }
    }

    public static class AgentMemory {
        private boolean enabled;

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class ComplianceWorkflow {
        private boolean enabled;

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class Security {
        private boolean enabled = true;
        private String username = "admin";
        private String password = "admin123";

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        public String getUsername() {
            return username;
        }

        public void setUsername(String username) {
            this.username = username;
        }

        public String getPassword() {
            return password;
        }

        public void setPassword(String password) {
            this.password = password;
        }
    }

    public static class Oidc {
        private boolean enabled;
        private String tenantClaim = "tenant_id";
        private String rolesClaim = "roles";

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }

        public String getTenantClaim() {
            return tenantClaim;
        }

        public void setTenantClaim(String tenantClaim) {
            this.tenantClaim = tenantClaim;
        }

        public String getRolesClaim() {
            return rolesClaim;
        }

        public void setRolesClaim(String rolesClaim) {
            this.rolesClaim = rolesClaim;
        }
    }

    public static class Opa {
        private boolean enabled;
        private String baseUrl = "http://localhost:8181";
        private String decisionPath = "agent_platform/llm/allow";

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

        public String getDecisionPath() {
            return decisionPath;
        }

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

        public String getSystem() {
            return system;
        }

        public void setSystem(String system) {
            this.system = system;
        }

        public String getUser() {
            return user;
        }

        public void setUser(String user) {
            this.user = user;
        }
    }
}
