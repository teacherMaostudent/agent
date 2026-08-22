package com.zxf.ai.gateway.admin;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.Map;

@Component
public class ModelConfigService {
    private static final String PROVIDER_KIND = "config-provider";
    private static final String MODEL_KIND = "config-model";
    private static final String ROUTE_KIND = "config-route";
    private static final String QUOTA_KIND = "config-quota";

    private final GatewayProperties properties;
    private final RuntimeStateRepository stateRepository;

    /**
     * 初始化 model config service 所需的依赖与运行期状态。
    */
    public ModelConfigService(GatewayProperties properties, ObjectProvider<RuntimeStateRepository> stateRepository) {
        // Restore durable overrides at startup so administrator changes survive a process restart.
        this.properties = properties;
        this.stateRepository = stateRepository.getIfAvailable();
        loadPersistedOverrides();
    }

    /**
     * 执行 upsert provider 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public synchronized Map<String, Object> upsertProvider(String providerName, GatewayProperties.Provider provider) {
        // Update one provider atomically and persist the same logical change when a state store exists.
        properties.getProviders().put(providerName, provider);
        if (stateRepository != null) {
            stateRepository.saveDocument(PROVIDER_KIND, providerName, new ProviderOverride(providerName, provider));
        }
        return Map.of("updated", true, "provider", providerName, "store", stateRepository == null ? "memory" : "mysql");
    }

    /**
     * 执行 upsert model 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public synchronized Map<String, Object> upsertModel(String providerName, String modelName, GatewayProperties.Model model) {
        // Add or replace a model only beneath an existing provider; never create a typoed provider implicitly.
        GatewayProperties.Provider provider = properties.getProviders().get(providerName);
        if (provider == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown provider: " + providerName);
        }
        provider.getModels().put(modelName, model);
        if (stateRepository != null) {
            stateRepository.saveDocument(MODEL_KIND, providerName + ":" + modelName, new ModelOverride(providerName, modelName, model));
        }
        return Map.of("updated", true, "provider", providerName, "model", modelName, "store", stateRepository == null ? "memory" : "mysql");
    }

    /**
     * 执行受控的 delete model 清理操作，并将状态变更交由对应服务持久化。
    */
    public synchronized Map<String, Object> deleteModel(String providerName, String modelName) {
        // Remove memory and durable state so a deleted model cannot reappear after restart.
        GatewayProperties.Provider provider = properties.getProviders().get(providerName);
        if (provider == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown provider: " + providerName);
        }
        provider.getModels().remove(modelName);
        if (stateRepository != null) {
            stateRepository.deleteDocument(MODEL_KIND, providerName + ":" + modelName);
        }
        return Map.of("deleted", true, "provider", providerName, "model", modelName, "store", stateRepository == null ? "memory" : "mysql");
    }

    /**
     * 执行 upsert route 的创建或更新，并保持运行期配置与持久化状态一致。
    */
    public synchronized Map<String, Object> upsertRoute(String routeName, GatewayProperties.Route route) {
        // Store an atomic logical-route update; release governance separately decides whether it may be used.
        properties.getRoutes().put(routeName, route);
        if (stateRepository != null) {
            stateRepository.saveDocument(ROUTE_KIND, routeName, new RouteOverride(routeName, route));
        }
        return Map.of("updated", true, "route", routeName, "store", stateRepository == null ? "memory" : "mysql");
    }

    /**
     * 执行受控的 delete route 清理操作，并将状态变更交由对应服务持久化。
    */
    public synchronized Map<String, Object> deleteRoute(String routeName) {
        // Delete the runtime override and durable record so future requests fail closed instead of using stale policy.
        properties.getRoutes().remove(routeName);
        if (stateRepository != null) {
            stateRepository.deleteDocument(ROUTE_KIND, routeName);
        }
        return Map.of("deleted", true, "route", routeName, "store", stateRepository == null ? "memory" : "mysql");
    }

    /** Return only one tenant's quota policy; provider credentials and other tenants stay hidden. */
    public synchronized Map<String, GatewayProperties.UserQuota> quotas(String tenantId) {
        String prefix = tenantId + ":";
        return properties.getUserQuotas().entrySet().stream()
                .filter(entry -> entry.getKey().startsWith(prefix))
                .collect(java.util.stream.Collectors.toMap(
                        entry -> entry.getKey().substring(prefix.length()),
                        Map.Entry::getValue,
                        (left, right) -> right,
                        java.util.LinkedHashMap::new
                ));
    }

    /** Atomically replace one tenant's desired user/default quotas and persist every override. */
    public synchronized Map<String, Object> replaceQuotas(
            String tenantId, Map<String, GatewayProperties.UserQuota> quotas) {
        if (tenantId == null || tenantId.isBlank() || quotas.size() > 1_000) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Invalid tenant or quota count");
        }
        quotas.forEach((userId, quota) -> {
            if (userId == null || userId.isBlank() || quota == null
                    || quota.getDailyTokenLimit() < 1
                    || quota.getDailyCostLimit() == null
                    || quota.getDailyCostLimit().signum() <= 0) {
                throw new GatewayException(HttpStatus.BAD_REQUEST, "Quota limits and user IDs must be positive");
            }
        });
        String prefix = tenantId + ":";
        var removed = properties.getUserQuotas().keySet().stream()
                .filter(key -> key.startsWith(prefix)).toList();
        removed.forEach(key -> {
            properties.getUserQuotas().remove(key);
            if (stateRepository != null) stateRepository.deleteDocument(QUOTA_KIND, key);
        });
        quotas.forEach((userId, quota) -> {
            String key = prefix + userId;
            properties.getUserQuotas().put(key, quota);
            if (stateRepository != null) {
                stateRepository.saveDocument(QUOTA_KIND, key, new QuotaOverride(key, quota));
            }
        });
        return Map.of(
                "updated", true,
                "tenantId", tenantId,
                "quotas", quotas(tenantId),
                "store", stateRepository == null ? "memory" : "postgres"
        );
    }

    /**
     * 构建或解析 load persisted overrides 所需的受控对象，避免调用方依赖内部实现细节。
    */
    private void loadPersistedOverrides() {
        // Rehydrate providers before models, then routes that reference the completed provider catalog.
        if (stateRepository == null) {
            return;
        }
        stateRepository.listDocuments(PROVIDER_KIND, ProviderOverride.class)
                .forEach(item -> properties.getProviders().put(item.providerName(), item.provider()));
        stateRepository.listDocuments(MODEL_KIND, ModelOverride.class)
                .forEach(item -> {
                    GatewayProperties.Provider provider = properties.getProviders().get(item.providerName());
                    if (provider != null) {
                        provider.getModels().put(item.modelName(), item.model());
                    }
                });
        stateRepository.listDocuments(ROUTE_KIND, RouteOverride.class)
                .forEach(item -> properties.getRoutes().put(item.routeName(), item.route()));
        stateRepository.listDocuments(QUOTA_KIND, QuotaOverride.class)
                .forEach(item -> properties.getUserQuotas().put(item.subject(), item.quota()));
    }

    /**
     * 定义供应商级持久化覆盖项，内容在应用前接受配置校验。
    */
    public record ProviderOverride(String providerName, GatewayProperties.Provider provider) {
    }

    /**
     * 定义模型级持久化覆盖项，键由 provider 与 modelName 共同确定。
    */
    public record ModelOverride(String providerName, String modelName, GatewayProperties.Model model) {
    }

    /**
     * 定义路由级持久化覆盖项，包含固定 primary、fallback 与 canary 策略。
    */
    public record RouteOverride(String routeName, GatewayProperties.Route route) {
    }

    /** Tenant-qualified quota override; subject uses ``tenant:user`` or ``tenant:*``. */
    public record QuotaOverride(String subject, GatewayProperties.UserQuota quota) {
    }
}
