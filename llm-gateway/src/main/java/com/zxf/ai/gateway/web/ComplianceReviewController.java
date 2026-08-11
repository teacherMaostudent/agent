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

    /**
     * 初始化 compliance review controller 所需的依赖与运行期状态。
    */
    public ComplianceReviewController(PlatformServiceClient platform, ApiKeyService apiKeyService) {
        this.platform = platform;
        this.apiKeyService = apiKeyService;
    }

    @PostMapping(path = "/v1/compliance/reviews",
            consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    /**
     * 执行 create review 的创建或更新，并保持运行期配置与持久化状态一致。
    */
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
    /**
     * 执行 snapshot 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> snapshot(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/compliance",
                null, tenant, user);
    }

    @GetMapping("/admin/compliance/reviews")
    /**
     * 执行 reviews 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> reviews(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET", "/v1/governance/compliance/reviews",
                null, tenant, user);
    }

    @GetMapping("/admin/compliance/reviews/{reviewId}")
    /**
     * 执行 review 对应的受控业务步骤，并保持网关边界与状态约束。
    */
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
    /**
     * 执行 confirm 对应的受控业务步骤，并保持网关边界与状态约束。
    */
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
    /**
     * 执行 audit logs 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Mono<JsonNode> auditLogs(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.governance("GET",
                "/v1/governance/compliance/audit-logs", null, tenant, user);
    }
}
