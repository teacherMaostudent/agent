package com.zxf.ai.gateway.routing;

import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

@Component
public class ModelRouter {
    private final GatewayProperties properties;

    public ModelRouter(GatewayProperties properties) {
        this.properties = properties;
    }

    /**
     * 将业务侧传入的逻辑模型名解析成实际可调用的模型端点列表。
     *
     * <p>业务侧看到的是 route 名，例如 deepseek-v4-flash、qwen-plus、kimi-chat。
     * 网关内部真正调用的是 provider:model，例如 deepseek:deepseek-v4-flash。</p>
     *
     * <p>返回值是有顺序的调用计划：</p>
     * <ol>
     *     <li>第 1 个端点是本次选择出来的主调目标，可能来自 primary、weighted 或 canary。</li>
     *     <li>后续端点来自 fallbacks，当前面端点失败时按顺序降级尝试。</li>
     * </ol>
     *
     * <p>四类路由配置的语义：</p>
     * <ul>
     *     <li>primary：默认主模型，适合稳定、成本和效果都经过验证的模型。</li>
     *     <li>weighted：权重路由，适合多模型负载均衡、成本优化、A/B 分流。</li>
     *     <li>canary：灰度路由，适合小流量验证新模型、新供应商或新参数配置。</li>
     *     <li>fallbacks：故障降级链路，适合主模型超时、限流、熔断或供应商异常时兜底。</li>
     * </ul>
     */
    public List<ModelEndpoint> resolve(String requestedModel) {
        // 调用方没有传 model 时使用 default-model，降低业务接入成本。
        String model = requestedModel == null || requestedModel.isBlank()
                ? properties.getDefaultModel()
                : requestedModel;
        GatewayProperties.Route route = properties.getRoutes().get(model);
        List<String> routeKeys = new ArrayList<>();
        if (route == null) {
            // 没有 route 配置时，支持两种兼容写法：
            // 1. provider:model，例如 deepseek:deepseek-v4-flash，直接当作真实端点。
            // 2. model，例如 gpt-4o-mini，默认归到 openai provider，兼容 OpenAI 客户端习惯。
            routeKeys.add(model.contains(":") ? model : "openai:" + model);
        } else {
            routeKeys.add(selectPrimary(route));
            routeKeys.addAll(route.getFallbacks());
        }
        return routeKeys.stream().map(this::toEndpoint).toList();
    }

    /**
     * 从 primary、weighted、canary 中选出本次请求的第一调用目标。
     *
     * <p>优先级是 canary -> weighted -> primary：</p>
     * <ul>
     *     <li>canary 优先：如果命中灰度百分比，本次请求进入灰度模型。</li>
     *     <li>weighted 其次：没有命中灰度时，按权重在多个目标中随机选择。</li>
     *     <li>primary 兜底：没有配置灰度/权重，或权重总和无效时，使用 primary。</li>
     * </ul>
     */
    private String selectPrimary(GatewayProperties.Route route) {
        for (GatewayProperties.CanaryTarget canary : route.getCanary()) {
            int percent = Math.max(0, Math.min(100, canary.getPercent()));
            if (percent > 0 && ThreadLocalRandom.current().nextInt(100) < percent) {
                // canary 表示灰度目标，percent=5 就是约 5% 请求打到新模型。
                return canary.getTarget();
            }
        }
        if (route.getWeighted() == null || route.getWeighted().isEmpty()) {
            // 没有权重配置时，使用最稳定的 primary。
            return route.getPrimary();
        }
        int total = route.getWeighted().stream()
                .mapToInt(target -> Math.max(0, target.getWeight()))
                .sum();
        if (total <= 0) {
            // 全部权重都小于等于 0 属于无效配置，回退 primary，避免随机失败。
            return route.getPrimary();
        }
        int cursor = ThreadLocalRandom.current().nextInt(total);
        for (GatewayProperties.WeightedTarget target : route.getWeighted()) {
            cursor -= Math.max(0, target.getWeight());
            if (cursor < 0) {
                // weighted 表示按权重抽样。权重越大，被选中的概率越高。
                return target.getTarget();
            }
        }
        return route.getPrimary();
    }

    /**
     * 将 provider:model 字符串解析成 ModelEndpoint。
     *
     * <p>ModelEndpoint 会同时携带 provider 配置和 model 配置：
     * provider 决定 base-url、api-key、protocol；model 决定 upstream-model 和单价。</p>
     */
    private ModelEndpoint toEndpoint(String routeKey) {
        // routeKey 的格式固定为 provider:model，例如 deepseek:deepseek-v4-flash。
        String[] parts = routeKey.split(":", 2);
        if (parts.length != 2) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Invalid route key: " + routeKey);
        }
        GatewayProperties.Provider provider = properties.getProviders().get(parts[0]);
        if (provider == null) {
            // provider 不存在通常是 application.yml 或 Admin 热更新配置错误。
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown provider: " + parts[0]);
        }
        GatewayProperties.Model model = provider.getModels().get(parts[1]);
        if (model == null) {
            // provider 存在但 model 不存在，说明 route 指向了一个未注册模型。
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown model route: " + routeKey);
        }
        return new ModelEndpoint(parts[0], parts[1], model.getUpstreamModel(), provider, model);
    }
}
