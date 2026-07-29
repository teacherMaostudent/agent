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

    public EvaluationGovernanceController(PlatformServiceClient platform, ApiKeyService apiKeyService) {
        this.platform = platform;
        this.apiKeyService = apiKeyService;
    }

    @PostMapping("/v1/feedback")
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
    public Mono<JsonNode> snapshot(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/evaluations/online",
                null, tenant, user);
    }

    @PostMapping("/admin/eval/governance/samples/{sampleId}/judge")
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
