package com.zxf.ai.gateway.auth;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.List;

@Component
public class ApiKeyService {
    private final GatewayProperties properties;

    public ApiKeyService(GatewayProperties properties) {
        this.properties = properties;
    }

    public AuthResult authenticate(String authorization, String xApiKey, String requestedUserId, String requestedModel) {
        return authenticate(authorization, xApiKey, null, requestedUserId, requestedModel);
    }

    public AuthResult authenticate(String authorization, String xApiKey, String requestedTenantId,
                                   String requestedUserId, String requestedModel) {
        String rawKey = extractKey(authorization, xApiKey);
        if (rawKey == null || rawKey.isBlank()) {
            if (!properties.isAllowAnonymous()) {
                throw new GatewayException(HttpStatus.UNAUTHORIZED, "API key is required");
            }
            // 保留 public/anonymous 模式，方便本地演示和内部低风险接口接入。
            // 对外生产环境通常会关闭匿名访问，或在网关前再加统一鉴权。
            return new AuthResult("public", requestedUserId == null || requestedUserId.isBlank() ? "anonymous" : requestedUserId, false);
        }
        GatewayProperties.ApiKey apiKey = properties.getApiKeys().get(rawKey);
        if (apiKey == null || !apiKey.isEnabled()) {
            throw new GatewayException(HttpStatus.UNAUTHORIZED, "Invalid or disabled API key");
        }
        List<String> allowedModels = apiKey.getAllowedModels();
        if (requestedModel != null && !requestedModel.isBlank()
                && allowedModels != null && !allowedModels.isEmpty()
                && !allowedModels.contains(requestedModel)) {
            // 模型白名单在 API Key 层做，可以限制不同租户只能访问指定成本/能力等级的模型。
            throw new GatewayException(HttpStatus.FORBIDDEN, "API key is not allowed to use model: " + requestedModel);
        }
        String tenantId = apiKey.isTrustedService()
                && requestedTenantId != null && !requestedTenantId.isBlank()
                ? requestedTenantId.trim() : apiKey.getTenantId();
        return new AuthResult(tenantId, apiKey.getUserId(), true);
    }

    public Object snapshot() {
        // 管理接口只返回脱敏后的 key，避免把配置文件中的明文密钥再次暴露出去。
        return properties.getApiKeys().entrySet().stream()
                .map(entry -> new ApiKeyView(mask(entry.getKey()), entry.getValue().getTenantId(),
                        entry.getValue().getUserId(), entry.getValue().isEnabled(),
                        entry.getValue().isTrustedService(), entry.getValue().getAllowedModels()))
                .toList();
    }

    private String extractKey(String authorization, String xApiKey) {
        // 同时支持 X-Api-Key 和 Authorization: Bearer，方便兼容常见 API Gateway 调用方式。
        if (xApiKey != null && !xApiKey.isBlank()) {
            return xApiKey.trim();
        }
        if (authorization != null && authorization.regionMatches(true, 0, "Bearer ", 0, 7)) {
            return authorization.substring(7).trim();
        }
        return null;
    }

    private String mask(String key) {
        if (key == null || key.length() <= 8) {
            return "****";
        }
        return key.substring(0, 4) + "****" + key.substring(key.length() - 4);
    }

    public record AuthResult(String tenantId, String userId, boolean authenticated) {
    }

    private record ApiKeyView(String key, String tenantId, String userId, boolean enabled,
                              boolean trustedService, List<String> allowedModels) {
    }
}
