package com.zxf.ai.gateway.config;

import org.springframework.beans.factory.InitializingBean;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.time.Duration;
import java.util.List;

@Component
public class ProductionConfigurationValidator implements InitializingBean {
    private final GatewayProperties properties;
    private final Environment environment;

    /**
     * 初始化 production configuration validator 所需的依赖与运行期状态。
    */
    public ProductionConfigurationValidator(GatewayProperties properties, Environment environment) {
        this.properties = properties;
        this.environment = environment;
    }

    @Override
    /**
     * 执行 after properties set 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public void afterPropertiesSet() {
        String deployment = environment.getProperty("DEPLOYMENT_ENVIRONMENT", "local");
        if (!deployment.equalsIgnoreCase("production") && !deployment.equalsIgnoreCase("prod")) {
            return;
        }
        List<String> unsafe = new ArrayList<>();
        if (properties.isAllowAnonymous()) unsafe.add("GATEWAY_ALLOW_ANONYMOUS must be false");
        if (!"redis".equalsIgnoreCase(properties.getQuotaStore())) unsafe.add("GATEWAY_QUOTA_STORE must be redis");
        if (!"redis".equalsIgnoreCase(properties.getAdmission().getStore())) unsafe.add("GATEWAY_ADMISSION_STORE must be redis");
        if (properties.getMaxRetries() != 0) unsafe.add("gateway.max-retries must be 0; use the bounded fallback chain instead");
        GatewayProperties.Admission admission = properties.getAdmission();
        long maxRequestTokens = admission.getMaxPromptTokens() + admission.getMaxCompletionTokens();
        validateTpm("GATEWAY_TENANT_TPM", admission.getTenantTokensPerMinute(), maxRequestTokens, unsafe);
        validateTpm("GATEWAY_USER_TPM", admission.getUserTokensPerMinute(), maxRequestTokens, unsafe);
        validateTpm("GATEWAY_ROUTE_TPM", admission.getRouteTokensPerMinute(), maxRequestTokens, unsafe);
        validateTpm("GATEWAY_PROVIDER_TPM", admission.getProviderTokensPerMinute(), maxRequestTokens, unsafe);
        if (admission.getMaxUpstreamAttempts() < 1) unsafe.add("GATEWAY_MAX_UPSTREAM_ATTEMPTS must be at least 1");
        long minimumLeaseTtl = properties.getRequestTimeout().multipliedBy(admission.getMaxUpstreamAttempts())
                .plus(Duration.ofSeconds(15)).toSeconds();
        if (admission.getConcurrencyLeaseTtlSeconds() < minimumLeaseTtl) {
            unsafe.add("GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS must cover request-timeout * max-upstream-attempts + 15s");
        }
        GatewayProperties.Security admin = properties.getAdmin().getSecurity();
        if (!admin.isEnabled()) unsafe.add("ADMIN_SECURITY_ENABLED must be true");
        if ("admin123".equals(admin.getPassword())) unsafe.add("ADMIN_PASSWORD must be rotated");
        if (!properties.getPersistence().isEnabled()) unsafe.add("GATEWAY_PERSISTENCE_ENABLED must be true");
        if (!properties.getOidc().isEnabled()) unsafe.add("GATEWAY_OIDC_ENABLED must be true");
        if (!properties.getOpa().isEnabled()) unsafe.add("GATEWAY_OPA_ENABLED must be true");
        if (environment.getProperty("WORKLOAD_IDENTITY_TOKEN_URL", "").isBlank()
                || environment.getProperty("WORKLOAD_IDENTITY_CLIENT_SECRET", "").isBlank()) {
            unsafe.add("WORKLOAD_IDENTITY_TOKEN_URL and WORKLOAD_IDENTITY_CLIENT_SECRET are required");
        }
        if (!unsafe.isEmpty()) {
            throw new IllegalStateException("Unsafe production configuration: " + String.join("; ", unsafe));
        }
    }

    /** 避免一条已通过输入校验的请求因 TPM 小于最大请求而永远无法获得令牌。 */
    private void validateTpm(String name, long limit, long maxRequestTokens, List<String> unsafe) {
        if (limit > 0 && limit < maxRequestTokens) {
            unsafe.add(name + " must be at least max-prompt-tokens + max-completion-tokens");
        }
    }
}
