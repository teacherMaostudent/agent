package com.zxf.ai.gateway.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.auth.WorkloadIdentityService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatusCode;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

/**
 * Narrow client boundary for Governance and Control Plane.
 *
 * <p>It attaches workload identity and the temporary service credentials in
 * one place, so application services do not construct cross-platform requests
 * themselves.  Credential headers are retained only for migration compatibility
 * while OIDC/mTLS becomes the mandatory production transport identity.</p>
 */
@Component
public class PlatformServiceClient {
    private final WebClient governance;
    private final WebClient controlPlane;
    private final ObjectMapper objectMapper;
    private final String defaultTenant;
    private final String governanceAuditorKey;
    private final String governanceEventKey;
    private final String controlPlaneAdminKey;
    private final WorkloadIdentityService workloadIdentity;

    /** 创建通往 Governance 与 Control Plane 的独立客户端，并集中保存工作负载身份配置。 */
    public PlatformServiceClient(
            WebClient.Builder builder,
            ObjectMapper objectMapper,
            @Value("${platform-services.governance.base-url:http://localhost:8085}") String governanceUrl,
            @Value("${platform-services.control-plane.base-url:http://localhost:8086}") String controlPlaneUrl,
            @Value("${platform-services.default-tenant:demo-tenant}") String defaultTenant,
            @Value("${platform-services.governance.auditor-key:}") String governanceAuditorKey,
            @Value("${platform-services.governance.event-key:}") String governanceEventKey,
            @Value("${platform-services.control-plane.admin-key:}") String controlPlaneAdminKey,
            WorkloadIdentityService workloadIdentity
    ) {
        this.governance = builder.clone().baseUrl(governanceUrl).build();
        this.controlPlane = builder.clone().baseUrl(controlPlaneUrl).build();
        this.objectMapper = objectMapper;
        this.defaultTenant = defaultTenant;
        this.governanceAuditorKey = governanceAuditorKey;
        this.governanceEventKey = governanceEventKey;
        this.controlPlaneAdminKey = controlPlaneAdminKey;
        this.workloadIdentity = workloadIdentity;
    }

    /** 将规范化事件投递给 Governance 事件入口，身份由服务工作负载凭据证明。 */
    public Mono<JsonNode> governanceEvent(JsonNode body, String tenantId, String userId) {
        return exchange(governance, "POST", "/v1/governance/events", body, tenantId, userId,
                "event-producer", "X-Governance-Event-Key", governanceEventKey);
    }

    /** 以审计角色调用 Governance API，不允许业务服务自行拼装认证头。 */
    public Mono<JsonNode> governance(String method, String path, JsonNode body,
                                     String tenantId, String userId) {
        return exchange(governance, method, path, body, tenantId, userId,
                "governance-auditor", "X-Governance-Auditor-Key", governanceAuditorKey);
    }

    /** 以受限管理员角色调用 Control Plane API，用于受治理的发布查询和校验。 */
    public Mono<JsonNode> controlPlane(String method, String path, JsonNode body,
                                      String tenantId, String userId) {
        return exchange(controlPlane, method, path, body, tenantId, userId,
                "agent-admin", "X-Control-Plane-Admin-Key", controlPlaneAdminKey);
    }

    /** 统一添加工作负载令牌、租户关联与过渡凭据，并把非成功状态转换为平台异常。 */
    private Mono<JsonNode> exchange(WebClient client, String method, String path, JsonNode body,
                                    String tenantId, String userId, String role,
                                    String credentialHeader, String credential) {
        return workloadIdentity.authorizationHeader().flatMap(authorization -> {
            WebClient.RequestBodySpec request = client.method(org.springframework.http.HttpMethod.valueOf(method))
                    .uri(path)
                    .header("X-Tenant-Id", value(tenantId, defaultTenant))
                    .header("X-User-Id", value(userId, "llm-gateway"))
                    .header("X-Roles", role);
            if (!authorization.isBlank()) request.header(HttpHeaders.AUTHORIZATION, authorization);
            if (credential != null && !credential.isBlank()) request.header(credentialHeader, credential);
            WebClient.RequestHeadersSpec<?> prepared = body == null ? request : request.bodyValue(body);
            return prepared.exchangeToMono(response -> {
            HttpStatusCode status = response.statusCode();
            return response.bodyToMono(String.class).defaultIfEmpty("{}").flatMap(payload -> {
                JsonNode parsed;
                try {
                    parsed = objectMapper.readTree(payload);
                } catch (Exception ignored) {
                    parsed = objectMapper.createObjectNode().put("message", payload);
                }
                if (status.isError()) {
                    return Mono.error(new PlatformServiceException(status.value(), parsed));
                }
                return Mono.just(parsed);
            });
            });
        });
    }

    /** 在调用方未提供可用值时选择配置默认值，避免空身份头传递到下游。 */
    private String value(String candidate, String fallback) {
        return candidate == null || candidate.isBlank() ? fallback : candidate;
    }

    public static final class PlatformServiceException extends RuntimeException {
        private final int status;
        private final JsonNode body;

        /** 保存下游 HTTP 状态与已解析错误体，便于上层按状态实施重试或降级。 */
        public PlatformServiceException(int status, JsonNode body) {
            super("Platform service returned HTTP " + status);
            this.status = status;
            this.body = body;
        }

        /** 返回下游服务返回的 HTTP 状态码。 */
        public int status() {
            return status;
        }

        /** 返回下游服务错误响应的结构化内容。 */
        public JsonNode body() {
            return body;
        }
    }
}
