package com.zxf.ai.gateway.admission;

/**
 * 一次准入成功后持有的并发许可。
 *
 * <p>频率和 TPM 配额在准入时已经消耗，只有 in-flight 计数需要在响应完成、取消或异常时归还。</p>
 */
@FunctionalInterface
public interface AdmissionLease {
    /** 幂等地释放此次调用占用的并发许可。 */
    void release();

    /** 未启用限流或没有并发维度时使用的空租约。 */
    AdmissionLease NOOP = () -> { };
}
