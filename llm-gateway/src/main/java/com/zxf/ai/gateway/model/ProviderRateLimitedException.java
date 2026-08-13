package com.zxf.ai.gateway.model;

import org.springframework.http.HttpStatus;

/**
 * 上游模型厂商返回的 429。
 *
 * <p>它与网关自身的准入拒绝刻意使用不同类型和原因码：前者允许路由层选择备用
 * Provider，后者表示本系统已经按租户、用户或路由策略阻止了请求。</p>
 */
public final class ProviderRateLimitedException extends GatewayException {
    private final String provider;
    private final String route;
    private final long retryAfterSeconds;

    /** 构造不包含供应商响应体的安全 429，避免把上游敏感错误内容透传给调用方。 */
    public ProviderRateLimitedException(String provider, String route, long retryAfterSeconds) {
        super(HttpStatus.TOO_MANY_REQUESTS, "Upstream provider rate limit exceeded");
        this.provider = provider;
        this.route = route;
        this.retryAfterSeconds = Math.max(1, retryAfterSeconds);
    }

    /** 返回受限上游的稳定 Provider 标识，供内部监控而非客户端展示使用。 */
    public String provider() { return provider; }

    /** 返回发生限制的发布路由标识，供回退与审计关联使用。 */
    public String route() { return route; }

    /** 返回上游声明或保守推导的最短退避秒数。 */
    public long retryAfterSeconds() { return retryAfterSeconds; }

    /** 返回与网关本地限流不同的稳定机器可读原因码。 */
    public String reasonCode() { return "PROVIDER_RATE_LIMIT"; }
}
