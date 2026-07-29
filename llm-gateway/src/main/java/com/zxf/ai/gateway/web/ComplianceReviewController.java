package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

/** Compatibility facade for the Governance-owned compliance workflow. */
@RestController
public class ComplianceReviewController {
    private final PlatformServiceClient platform;
    private final ApiKeyService apiKeyService;

    public ComplianceReviewController(PlatformServiceClient platform, ApiKeyService apiKeyService) {
        this.platform = platform;
        this.apiKeyService = apiKeyService;
    }

    @PostMapping(path = "/v1/compliance/reviews",
            consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    public Mono<JsonNode> createReview(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestHeader(value = "X-Api-Key", required = false) String xApiKey,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestBody JsonNode request
    ) {
        String model = request.path("model").asText(null);
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(
                authorization, xApiKey, userId, model);
        return platform.governance("POST", "/v1/governance/compliance/reviews",
                request, auth.tenantId(), auth.userId());
    }

    @GetMapping("/admin/compliance")
    public Mono<JsonNode> snapshot(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/compliance",
                null, tenant, user);
    }

    @GetMapping("/admin/compliance/reviews")
    public Mono<JsonNode> reviews(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/compliance/reviews",
                null, tenant, user);
    }

    @GetMapping("/admin/compliance/reviews/{reviewId}")
    public Mono<JsonNode> review(
            @PathVariable String reviewId,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET",
                "/v1/governance/compliance/reviews/" + reviewId,
                null, tenant, user);
    }

    @PostMapping("/admin/compliance/reviews/{reviewId}/confirm")
    public Mono<JsonNode> confirm(
            @PathVariable String reviewId,
            @RequestBody JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("POST",
                "/v1/governance/compliance/reviews/" + reviewId + "/confirm",
                request, tenant, user);
    }

    @GetMapping("/admin/compliance/audit-logs")
    public Mono<JsonNode> auditLogs(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET",
                "/v1/governance/compliance/audit-logs", null, tenant, user);
    }
}
