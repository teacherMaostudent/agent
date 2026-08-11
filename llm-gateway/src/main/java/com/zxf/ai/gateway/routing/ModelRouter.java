package com.zxf.ai.gateway.routing;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Resolves a logical model route into an ordered provider call plan.
 *
 * <p>The first endpoint is selected from canary, weighted or primary routing;
 * later endpoints are explicit fallbacks.  The gateway keeps this decision
 * local to a request, while the Control Plane owns versioned route policy.</p>
 */
@Component
public class ModelRouter {
    private final GatewayProperties properties;

    /** 注入已发布的路由目录；调用方不能借此传入供应商地址或凭据。 */
    public ModelRouter(GatewayProperties properties) {
        /** Retain the frozen route catalog; Runtime never supplies provider credentials or endpoints. */
        this.properties = properties;
    }

    /** 按逻辑模型名生成主路由与故障回退的有序调用计划。 */
    public List<ModelEndpoint> resolve(String requestedModel) {
        /** Build the ordered primary-plus-fallback plan for a non-pinned business request. */
        String model = requestedModel == null || requestedModel.isBlank()
                ? properties.getDefaultModel()
                : requestedModel;
        GatewayProperties.Route route = properties.getRoutes().get(model);
        List<String> routeKeys = new ArrayList<>();
        if (route == null) {
            // Preserve OpenAI-compatible callers: an unqualified model belongs
            // to the default provider, while provider:model is an explicit endpoint.
            routeKeys.add(model.contains(":") ? model : "openai:" + model);
        } else {
            routeKeys.add(selectPrimary(route));
            routeKeys.addAll(route.getFallbacks());
        }
        return routeKeys.stream().map(this::toEndpoint).toList();
    }

    /** 校验冻结路由版本和模型修订号，拒绝评测或治理请求发生配置漂移。 */
    public List<ModelEndpoint> resolvePinned(
            String requestedModel, String expectedRouteVersion, String expectedModelRevision) {
        if (expectedRouteVersion == null || expectedRouteVersion.isBlank()
                || expectedModelRevision == null || expectedModelRevision.isBlank()) {
            return resolve(requestedModel);
        }
        GatewayProperties.Route route = properties.getRoutes().get(requestedModel);
        if (route == null || !expectedRouteVersion.equals(route.getVersion())) {
            throw new GatewayException(HttpStatus.CONFLICT, "Pinned route version is unavailable");
        }
        List<ModelEndpoint> endpoints = resolve(requestedModel);
        if (endpoints.stream().anyMatch(endpoint -> !expectedModelRevision.equals(endpoint.model().getRevision()))) {
            throw new GatewayException(HttpStatus.CONFLICT, "Pinned model revision is unavailable");
        }
        return endpoints;
    }

    /** 依次应用灰度、权重和显式主路由规则，权重异常时安全回退到主路由。 */
    private String selectPrimary(GatewayProperties.Route route) {
        /** Apply canary first, then weighted routing, while preserving an explicit safe primary fallback. */
        for (GatewayProperties.CanaryTarget canary : route.getCanary()) {
            int percent = Math.max(0, Math.min(100, canary.getPercent()));
            if (percent > 0 && ThreadLocalRandom.current().nextInt(100) < percent) {
                return canary.getTarget();
            }
        }
        if (route.getWeighted() == null || route.getWeighted().isEmpty()) {
            return route.getPrimary();
        }
        int total = route.getWeighted().stream()
                .mapToInt(target -> Math.max(0, target.getWeight()))
                .sum();
        if (total <= 0) {
            // Invalid weights fail safely to the explicitly configured primary.
            return route.getPrimary();
        }
        int cursor = ThreadLocalRandom.current().nextInt(total);
        for (GatewayProperties.WeightedTarget target : route.getWeighted()) {
            cursor -= Math.max(0, target.getWeight());
            if (cursor < 0) {
                return target.getTarget();
            }
        }
        return route.getPrimary();
    }

    /** 将目录中的 provider:model 键解析为受配置约束的上游端点。 */
    private ModelEndpoint toEndpoint(String routeKey) {
        /** Resolve only configured provider:model pairs so callers cannot select arbitrary upstream URLs. */
        String[] parts = routeKey.split(":", 2);
        if (parts.length != 2) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Invalid route key: " + routeKey);
        }
        GatewayProperties.Provider provider = properties.getProviders().get(parts[0]);
        if (provider == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown provider: " + parts[0]);
        }
        GatewayProperties.Model model = provider.getModels().get(parts[1]);
        if (model == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown model route: " + routeKey);
        }
        return new ModelEndpoint(parts[0], parts[1], model.getUpstreamModel(), provider, model);
    }
}
