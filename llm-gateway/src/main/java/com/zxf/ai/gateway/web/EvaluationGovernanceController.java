package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

/** Compatibility facade for Governance-owned online evaluation. */
@RestController
@RequestMapping
public class EvaluationGovernanceController {
    private final PlatformServiceClient platform;
    private final ApiKeyService apiKeyService;

    /**
     * 初始化 evaluation governance controller 所需的依赖与运行期状态。
    */
    public EvaluationGovernanceController(PlatformServiceClient platform, ApiKeyService apiKeyService) {
        this.platform = platform;
        this.apiKeyService = apiKeyService;
    }

    @PostMapping("/v1/feedback")
    /**
     * 把用户反馈与 request_id、租户和主体身份绑定后写入 Governance 线上校准闭环。
    */
    public Mono<JsonNode> feedback(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestHeader(value = "X-Api-Key", required = false) String xApiKey,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody JsonNode request
    ) {
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(
                authorization, xApiKey, userId, null);
        return platform.governance("POST", "/v1/governance/evaluations/feedback",
                request, auth.tenantId(), auth.userId());
    }

    @GetMapping("/admin/eval/governance")
    /**
     * 返回当前组件的脱敏只读快照，调用不会推进业务状态或产生外部副作用。
    */
    public Mono<JsonNode> snapshot(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/evaluations/online",
                null, tenant, user);
    }

    @PostMapping("/admin/eval/governance/samples/{sampleId}/judge")
    /**
     * 请求 Governance 对指定线上样本执行冻结 Judge，并返回评判与人工复核处置。
    */
    public Mono<JsonNode> judge(
            @PathVariable String sampleId,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("POST",
                "/v1/governance/evaluations/online/samples/" + sampleId + "/judge",
                null, tenant, user);
    }

    @PostMapping("/admin/eval/governance/samples/{sampleId}/review")
    /**
     * 提交具名人工线上样本复核，决定进入校准集候选而非直接改变发布 Gate。
    */
    public Mono<JsonNode> reviewSample(
            @PathVariable String sampleId,
            @RequestBody JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("POST",
                "/v1/governance/evaluations/online/samples/" + sampleId + "/review",
                request, tenant, user);
    }

    @PostMapping("/admin/eval/governance/golden-candidates/{candidateId}/review")
    /**
     * 把专家对 Golden Candidate 的决定转发 Governance，批准后仍由 Governance 生成正式 Golden Case。
    */
    public Mono<JsonNode> reviewGoldenCandidate(
            @PathVariable String candidateId,
            @RequestBody JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("POST",
                "/v1/governance/evaluations/online/golden-candidates/"
                        + candidateId + "/review",
                request, tenant, user);
    }
}
