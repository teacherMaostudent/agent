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

    /**
     * 初始化 release controller 所需的依赖与运行期状态。
    */
    public ReleaseController(PlatformServiceClient platform) {
        this.platform = platform;
    }

    @PostMapping
    /**
     * 向 Control Plane 提交模型灰度发布请求，必须携带质量门禁和候选版本证据。
    */
    public Mono<JsonNode> start(
            @RequestBody JsonNode request,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("POST", "/v1/model-route-releases", request, tenant, user);
    }

    @PostMapping("/{releaseId}/monitor")
    /**
     * 请求 Control Plane 评估一次灰度发布指标并返回继续、晋级或回滚决定。
    */
    public Mono<JsonNode> monitor(
            @PathVariable String releaseId,
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("POST",
                "/v1/model-route-releases/" + releaseId + "/monitor", null, tenant, user);
    }

    @PostMapping("/{releaseId}/rollback")
    /**
     * 请求 Control Plane 恢复发布前记录的模型路由快照，并持久化人工原因。
    */
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
    /**
     * 列出 Control Plane 中当前租户的模型路由发布，不触发监控、晋级或回滚。
    */
    public Mono<JsonNode> list(
            @RequestHeader(value = "X-Tenant-Id", required = false) String tenant,
            @RequestHeader(value = "X-User-Id", required = false) String user
    ) {
        return platform.controlPlane("GET", "/v1/model-route-releases", null, tenant, user);
    }
}
