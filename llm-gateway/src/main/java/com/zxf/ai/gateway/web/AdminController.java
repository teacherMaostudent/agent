package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.admin.ModelConfigService;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.cache.RequestCacheService;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import com.zxf.ai.gateway.prompt.PromptTemplateService;
import com.zxf.ai.gateway.report.ModelPerformanceService;
import com.zxf.ai.gateway.report.UsageReportService;
import com.zxf.ai.gateway.resilience.GatewayPolicyService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.Map;
import reactor.core.publisher.Mono;

@RestController
@RequestMapping("/admin")
public class AdminController {
    private final GatewayProperties properties;
    private final ApiKeyService apiKeyService;
    private final RequestCacheService requestCacheService;
    private final PromptTemplateService promptTemplateService;
    private final GatewayPolicyService policyService;
    private final ModelConfigService modelConfigService;
    private final UsageReportService usageReportService;
    private final ModelPerformanceService performanceService;
    private final PlatformServiceClient platform;

    /**
     * 初始化 admin controller 所需的依赖与运行期状态。
    */
    public AdminController(
            GatewayProperties properties,
            ApiKeyService apiKeyService,
            RequestCacheService requestCacheService,
            PromptTemplateService promptTemplateService,
            GatewayPolicyService policyService,
            ModelConfigService modelConfigService,
            UsageReportService usageReportService,
            ModelPerformanceService performanceService,
            PlatformServiceClient platform
    ) {
        this.properties = properties;
        this.apiKeyService = apiKeyService;
        this.requestCacheService = requestCacheService;
        this.promptTemplateService = promptTemplateService;
        this.policyService = policyService;
        this.modelConfigService = modelConfigService;
        this.usageReportService = usageReportService;
        this.performanceService = performanceService;
        this.platform = platform;
    }

    @GetMapping("/overview")
    /**
     * 执行 overview 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Map<String, Object> overview() {
        // 管理后台首页聚合最常看的运行态数据，避免前端首屏需要连续请求多个接口。
        return Map.of(
                "providers", properties.getProviders().keySet(),
                "routes", properties.getRoutes().keySet(),
                "cache", requestCacheService.snapshot(),
                "resilience", policyService.snapshot(),
                "costReport", usageReportService.dailyReport(),
                "performance", performanceService.dailyReport()
        );
    }

    @GetMapping("/providers")
    /**
     * 执行 providers 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object providers() {
        // API Key 只暴露是否配置，不返回明文，避免管理接口意外泄露上游厂商密钥。
        return properties.getProviders().entrySet().stream()
                .collect(java.util.stream.Collectors.toMap(
                        Map.Entry::getKey,
                        entry -> Map.of(
                                "protocol", entry.getValue().getProtocol(),
                                "baseUrl", entry.getValue().getBaseUrl(),
                                "apiKeyConfigured", entry.getValue().getApiKey() != null && !entry.getValue().getApiKey().isBlank(),
                                "models", entry.getValue().getModels().keySet()
                        )
                ));
    }

    @PutMapping("/providers/{providerName}")
    /**
     * 执行 upsert provider 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Object upsertProvider(@PathVariable String providerName, @RequestBody GatewayProperties.Provider provider) {
        // 当前热更新直接修改运行期配置；生产环境通常还要写入配置中心或数据库。
        return modelConfigService.upsertProvider(providerName, provider);
    }

    @GetMapping("/routes")
    /**
     * 执行 routes 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object routes() {
        return properties.getRoutes();
    }

    @PutMapping("/routes/{routeName}")
    /**
     * 执行 upsert route 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Object upsertRoute(@PathVariable String routeName, @RequestBody GatewayProperties.Route route) {
        return modelConfigService.upsertRoute(routeName, route);
    }

    @DeleteMapping("/routes/{routeName}")
    /**
     * 执行受控的 delete route 清理操作，并将状态变更交由对应服务持久化。
    */
    public Object deleteRoute(@PathVariable String routeName) {
        return modelConfigService.deleteRoute(routeName);
    }

    @PutMapping("/providers/{providerName}/models/{modelName}")
    /**
     * 执行 upsert model 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Object upsertModel(
            @PathVariable String providerName,
            @PathVariable String modelName,
            @RequestBody GatewayProperties.Model model
    ) {
        return modelConfigService.upsertModel(providerName, modelName, model);
    }

    @DeleteMapping("/providers/{providerName}/models/{modelName}")
    /**
     * 执行受控的 delete model 清理操作，并将状态变更交由对应服务持久化。
    */
    public Object deleteModel(@PathVariable String providerName, @PathVariable String modelName) {
        return modelConfigService.deleteModel(providerName, modelName);
    }

    @GetMapping("/api-keys")
    /**
     * 执行 api keys 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object apiKeys() {
        return apiKeyService.snapshot();
    }

    @GetMapping("/prompt-templates")
    /**
     * 执行 prompt templates 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object promptTemplates() {
        return promptTemplateService.snapshot();
    }

    @GetMapping("/cache")
    /**
     * 执行 cache 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object cache() {
        return requestCacheService.snapshot();
    }

    @DeleteMapping("/cache")
    /**
     * 执行受控的 clear cache 清理操作，并将状态变更交由对应服务持久化。
    */
    public Map<String, Object> clearCache() {
        requestCacheService.clear();
        return Map.of("cleared", true);
    }

    @GetMapping("/models/health")
    /**
     * 执行 model health 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object modelHealth() {
        return policyService.snapshot();
    }

    @PostMapping("/models/probe")
    /**
     * 执行 probe models 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Map<String, Object> probeModels() {
        // 这里先提供轻量逻辑探测：展示网关侧熔断/限流状态。
        // 若要做真实探活，可按 provider 发送一个低成本 prompt 并记录延迟。
        return Map.of(
                "status", "logical-probe",
                "message", "Current lightweight probe reports circuit/rate-limit state. Active upstream ping can be added with a provider-specific prompt.",
                "health", policyService.snapshot()
        );
    }

    @GetMapping("/reports/cost")
    /**
     * 执行 cost report 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object costReport() {
        return usageReportService.report();
    }

    @GetMapping("/reports/cost/daily")
    /**
     * 执行 daily cost report 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object dailyCostReport() {
        return usageReportService.dailyReport();
    }

    @DeleteMapping("/reports/cost")
    /**
     * 执行受控的 clear cost report 清理操作，并将状态变更交由对应服务持久化。
    */
    public Map<String, Object> clearCostReport() {
        usageReportService.clear();
        return Map.of("cleared", true);
    }

    @GetMapping("/reports/performance")
    /**
     * 执行 performance report 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object performanceReport() {
        // 性能报表用于比较云端模型与本地推理服务的延迟、吞吐、错误率和成本。
        return performanceService.report();
    }

    @GetMapping("/reports/performance/daily")
    /**
     * 执行 daily performance report 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object dailyPerformanceReport() {
        return performanceService.dailyReport();
    }

    /** Read-only metric execution used by the Control Plane release controller. */
    @GetMapping("/reports/performance/summary")
    /**
     * 执行 performance summary 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object performanceSummary(
            @RequestParam Instant since,
            @RequestParam String requestedModel,
            @RequestParam String provider,
            @RequestParam String model
    ) {
        return performanceService.summarizeSince(since, requestedModel, provider, model);
    }

    @DeleteMapping("/reports/performance")
    /**
     * 执行受控的 clear performance report 清理操作，并将状态变更交由对应服务持久化。
    */
    public Map<String, Object> clearPerformanceReport() {
        performanceService.clear();
        return Map.of("cleared", true);
    }

    @GetMapping("/eval")
    /**
     * 执行 evaluation snapshot 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> evaluationSnapshot() {
        return platform.governance("GET", "/v1/governance/evaluations", null, null, null);
    }

    @PutMapping("/eval/prompt-versions")
    /**
     * 执行 upsert prompt version 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Mono<JsonNode> upsertPromptVersion(@RequestBody JsonNode request) {
        return platform.governance("PUT", "/v1/governance/evaluations/prompt-versions",
                request, null, null);
    }

    @PutMapping("/eval/retrieval-strategies")
    /**
     * 执行 upsert retrieval strategy 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Mono<JsonNode> upsertRetrievalStrategy(@RequestBody JsonNode request) {
        return platform.governance("PUT", "/v1/governance/evaluations/retrieval-strategies",
                request, null, null);
    }

    @PutMapping("/eval/golden-dataset")
    /**
     * 执行 upsert golden case 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Mono<JsonNode> upsertGoldenCase(@RequestBody JsonNode request) {
        return platform.governance("PUT", "/v1/governance/evaluations/golden-dataset",
                request, null, null);
    }

    @PostMapping("/eval/regression-runs")
    /**
     * 执行 run regression 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> runRegression(@RequestBody JsonNode request) {
        return platform.governance("POST", "/v1/governance/evaluations/regression-runs",
                request, null, null);
    }

    @PutMapping("/eval/judge-rubrics")
    /**
     * 执行 upsert judge rubric 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public Mono<JsonNode> upsertJudgeRubric(@RequestBody JsonNode request) {
        return platform.governance("PUT", "/v1/governance/evaluations/judge-rubrics",
                request, null, null);
    }

    @PostMapping("/eval/judge-runs")
    /**
     * 执行 run llm judge 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> runLlmJudge(@RequestBody JsonNode request) {
        return platform.governance("POST", "/v1/governance/evaluations/judge-runs",
                request, null, null);
    }

    @PostMapping("/eval/judge-runs/{runId}/quality-gate")
    /**
     * 执行 evaluate quality gate 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> evaluateQualityGate(
            @PathVariable String runId,
            @RequestBody(required = false) JsonNode request
    ) {
        return platform.governance("POST",
                "/v1/governance/evaluations/judge-runs/" + runId + "/quality-gate",
                request, null, null);
    }

    @PostMapping("/observability/phoenix/traces")
    /**
     * 执行 record phoenix trace 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> recordPhoenixTrace(@RequestBody JsonNode trace) {
        return platform.governance("POST", "/v1/governance/evaluations/traces",
                trace, null, null);
    }
}
