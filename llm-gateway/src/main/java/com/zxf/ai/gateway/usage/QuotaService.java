package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.model.GatewayUsage;

import java.math.BigDecimal;
import java.util.Map;

/**
 * 用户限额服务抽象。
 *
 * <p>业务层只依赖这个接口，不关心底层使用内存还是 Redis。这样本地开发可以用
 * MemoryQuotaService，生产多实例部署时切换到 RedisQuotaService。</p>
 */
public interface QuotaService {
    /**
     * 请求进入上游模型前预扣 prompt token。
     *
     * <p>注意这里是“预扣”，不是单纯检查。这样在多实例或高并发环境下，
     * 多个请求不会同时检查通过后一起打爆每日 token 限额。</p>
     */
    UsageReservation reserve(String userId, String requestId, long estimatedPromptTokens,
                             long estimatedCompletionTokens, BigDecimal estimatedCost);

    /**
     * 模型调用成功后追加 completion token 和成本。
     *
     * <p>prompt token 已经在 reserve 阶段扣过，所以实现类不应再次累计 prompt token。</p>
     */
    void settle(String userId, UsageReservation reservation, GatewayUsage gatewayUsage);

    /** 上游调用未形成可结算 usage 时释放预留；未知厂商费用由后续账单对账补记。 */
    void release(String userId, UsageReservation reservation);

    /**
     * 查询用户当天用量快照。
     */
    Map<String, Object> snapshot(String userId);
}
