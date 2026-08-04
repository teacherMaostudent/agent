package com.zxf.ai.gateway.config;

import org.springframework.beans.factory.InitializingBean;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

@Component
public class ProductionConfigurationValidator implements InitializingBean {
    private final GatewayProperties properties;
    private final Environment environment;

    public ProductionConfigurationValidator(GatewayProperties properties, Environment environment) {
        this.properties = properties;
        this.environment = environment;
    }

    @Override
    public void afterPropertiesSet() {
        String deployment = environment.getProperty("DEPLOYMENT_ENVIRONMENT", "local");
        if (!deployment.equalsIgnoreCase("production") && !deployment.equalsIgnoreCase("prod")) {
            return;
        }
        List<String> unsafe = new ArrayList<>();
        if (properties.isAllowAnonymous()) unsafe.add("GATEWAY_ALLOW_ANONYMOUS must be false");
        if (!"redis".equalsIgnoreCase(properties.getQuotaStore())) unsafe.add("GATEWAY_QUOTA_STORE must be redis");
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
}
