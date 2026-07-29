package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

/**
 * Backward-compatible facade. Model release state and decisions are owned by
 * agent-control-plane; Gateway only applies the resulting route configuration.
 */
@RestController
@RequestMapping("/admin/releases")
public class ReleaseController {
    private final PlatformServiceClient platform;

    public ReleaseController(PlatformServiceClient platform) {
        this.platform = platform;
    }

    @PostMapping
    public Mono<JsonNode> start(
            @RequestBody JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("POST", "/v1/model-route-releases", request, tenant, user);
    }

    @PostMapping("/{releaseId}/monitor")
    public Mono<JsonNode> monitor(
            @PathVariable String releaseId,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("POST",
                "/v1/model-route-releases/" + releaseId + "/monitor", null, tenant, user);
    }

    @PostMapping("/{releaseId}/rollback")
    public Mono<JsonNode> rollback(
            @PathVariable String releaseId,
            @RequestBody(required = false) JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("POST",
                "/v1/model-route-releases/" + releaseId + "/rollback", request, tenant, user);
    }

    @GetMapping
    public Mono<JsonNode> list(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("GET", "/v1/model-route-releases", null, tenant, user);
    }
}
