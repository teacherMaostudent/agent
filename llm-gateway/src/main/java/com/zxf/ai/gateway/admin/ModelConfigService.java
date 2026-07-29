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

    private final GatewayProperties properties;
    private final RuntimeStateRepository stateRepository;

    public ModelConfigService(GatewayProperties properties, ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.properties = properties;
        this.stateRepository = stateRepository.getIfAvailable();
        loadPersistedOverrides();
    }

    public synchronized Map<String, Object> upsertProvider(String providerName, GatewayProperties.Provider provider) {
        properties.getProviders().put(providerName, provider);
        if (stateRepository != null) {
            stateRepository.saveDocument(PROVIDER_KIND, providerName, new ProviderOverride(providerName, provider));
        }
        return Map.of("updated", true, "provider", providerName, "store", stateRepository == null ? "memory" : "mysql");
    }

    public synchronized Map<String, Object> upsertModel(String providerName, String modelName, GatewayProperties.Model model) {
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

    public synchronized Map<String, Object> deleteModel(String providerName, String modelName) {
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

    public synchronized Map<String, Object> upsertRoute(String routeName, GatewayProperties.Route route) {
        properties.getRoutes().put(routeName, route);
        if (stateRepository != null) {
            stateRepository.saveDocument(ROUTE_KIND, routeName, new RouteOverride(routeName, route));
        }
        return Map.of("updated", true, "route", routeName, "store", stateRepository == null ? "memory" : "mysql");
    }

    public synchronized Map<String, Object> deleteRoute(String routeName) {
        properties.getRoutes().remove(routeName);
        if (stateRepository != null) {
            stateRepository.deleteDocument(ROUTE_KIND, routeName);
        }
        return Map.of("deleted", true, "route", routeName, "store", stateRepository == null ? "memory" : "mysql");
    }

    private void loadPersistedOverrides() {
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
    }

    public record ProviderOverride(String providerName, GatewayProperties.Provider provider) {
    }

    public record ModelOverride(String providerName, String modelName, GatewayProperties.Model model) {
    }

    public record RouteOverride(String routeName, GatewayProperties.Route route) {
    }
}
