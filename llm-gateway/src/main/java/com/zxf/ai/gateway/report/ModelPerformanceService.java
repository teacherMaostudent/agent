package com.zxf.ai.gateway.report;

import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayUsage;
import com.zxf.ai.gateway.model.ModelEndpoint;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeoutException;

@Component
public class ModelPerformanceService {
    private static final String KIND = "performance-record";

    private final RuntimeStateRepository stateRepository;
    private final List<PerformanceRecord> records = new ArrayList<>();

    /** 选择可选持久化仓储；未配置时仅在单实例内存中保留性能记录。 */
    public ModelPerformanceService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

    /** 将成功调用的时延、首 Token、吞吐量和成本写入性能时间序列。 */
    public void recordSuccess(
            GatewayRequestContext context,
            ModelEndpoint endpoint,
            GatewayUsage usage,
            long latencyMs,
            Long ttftMs,
            Long tpotMs,
            BigDecimal tokensPerSecond
    ) {
        record(new PerformanceRecord(
                Instant.now(),
                context.requestId(),
                context.traceId(),
                context.tenantId(),
                context.userId(),
                context.agentId(),
                context.agentVersion(),
                context.sessionId(),
                context.runId(),
                context.purpose(),
                context.requestedModel(),
                endpoint.providerName(),
                endpoint.modelName(),
                endpoint.upstreamModel(),
                context.stream(),
                true,
                false,
                "",
                latencyMs,
                ttftMs,
                tpotMs,
                usage.promptTokens(),
                usage.completionTokens(),
                usage.totalTokens(),
                tokensPerSecond == null ? BigDecimal.ZERO : tokensPerSecond,
                usage.cost(),
                usage.totalTokens() == 0 ? BigDecimal.ZERO : usage.cost().divide(BigDecimal.valueOf(Math.max(1, usage.totalTokens())), 10, RoundingMode.HALF_UP)
        ));
    }

    /** 将失败调用及超时分类写入性能时间序列，不丢弃关联标识。 */
    public void recordFailure(GatewayRequestContext context, ModelEndpoint endpoint, Throwable error, long latencyMs) {
        record(new PerformanceRecord(
                Instant.now(),
                context.requestId(),
                context.traceId(),
                context.tenantId(),
                context.userId(),
                context.agentId(),
                context.agentVersion(),
                context.sessionId(),
                context.runId(),
                context.purpose(),
                context.requestedModel(),
                endpoint.providerName(),
                endpoint.modelName(),
                endpoint.upstreamModel(),
                context.stream(),
                false,
                isTimeout(error),
                error == null ? "unknown" : error.getMessage(),
                latencyMs,
                null,
                null,
                0,
                0,
                0,
                BigDecimal.ZERO,
                BigDecimal.ZERO,
                BigDecimal.ZERO
        ));
    }

    /** 生成全量性能汇总，并按提供商、模型和租户分组以定位退化来源。 */
    public Map<String, Object> report() {
        List<PerformanceRecord> snapshot = snapshot();
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "total", summarize(snapshot),
                "byProvider", group(snapshot, PerformanceRecord::provider),
                "byModel", group(snapshot, PerformanceRecord::model),
                "byTenant", group(snapshot, PerformanceRecord::tenantId),
                "recent", snapshot.stream().sorted((a, b) -> b.timestamp().compareTo(a.timestamp())).limit(100).toList()
        );
    }

    /** 生成按本地自然日切分的性能快照，用于日常 SLO 观察。 */
    public Map<String, Object> dailyReport() {
        LocalDate today = LocalDate.now();
        List<PerformanceRecord> todayRecords = snapshot().stream()
                .filter(record -> LocalDate.ofInstant(record.timestamp(), ZoneId.systemDefault()).equals(today))
                .toList();
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "date", today.toString(),
                "total", summarize(todayRecords),
                "byProvider", group(todayRecords, PerformanceRecord::provider),
                "byModel", group(todayRecords, PerformanceRecord::model)
        );
    }

    /** 汇总指定时间窗口内某一物理提供商与模型端点的指标，供发布质量门禁使用。 */
    public Summary summarizeSince(Instant since, String requestedModel, String provider, String model) {
        return summarize(snapshot().stream()
                .filter(record -> !record.timestamp().isBefore(since))
                .filter(record -> requestedModel.equals(record.requestedModel()))
                .filter(record -> provider.equals(record.provider()) && model.equals(record.model()))
                .toList());
    }

    /** 清空性能历史；持久化模式会删除对应种类文档，必须经运维授权调用。 */
    public void clear() {
        if (stateRepository != null) {
            stateRepository.deleteKind(KIND);
            return;
        }
        synchronized (records) {
            records.clear();
        }
    }

    /** 按部署模式持久化或追加一条性能记录，避免两种存储同时写入造成重复。 */
    private void record(PerformanceRecord record) {
        if (stateRepository != null) {
            stateRepository.saveDocument(KIND, record.requestId() + "-" + UUID.randomUUID(), record);
            return;
        }
        synchronized (records) {
            records.add(record);
        }
    }

    /** 取得稳定性能记录副本，避免报表汇总与并发写入共享可变集合。 */
    private List<PerformanceRecord> snapshot() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(KIND, PerformanceRecord.class);
        }
        synchronized (records) {
            return List.copyOf(records);
        }
    }

    /** 依据指定维度聚合性能记录，返回每个分组独立的统计器。 */
    private Map<String, Summary> group(List<PerformanceRecord> source, java.util.function.Function<PerformanceRecord, String> classifier) {
        Map<String, Summary> result = new LinkedHashMap<>();
        for (PerformanceRecord record : source) {
            result.computeIfAbsent(classifier.apply(record), ignored -> new Summary()).add(record);
        }
        return result;
    }

    /** 将一组性能记录折叠为指标汇总。 */
    private Summary summarize(List<PerformanceRecord> source) {
        Summary summary = new Summary();
        source.forEach(summary::add);
        return summary;
    }

    /** 根据异常类型和消息识别超时，供错误率与超时率分别统计。 */
    private boolean isTimeout(Throwable error) {
        if (error == null) {
            return false;
        }
        if (error instanceof TimeoutException) {
            return true;
        }
        String message = error.getMessage();
        return message != null && message.toLowerCase().contains("timeout");
    }

    /** 保存单次模型调用的可关联性能事实，作为聚合报表与发布门禁的原始依据。 */
    public record PerformanceRecord(
            Instant timestamp,
            String requestId,
            String traceId,
            String tenantId,
            String userId,
            String agentId,
            String agentVersion,
            String sessionId,
            String runId,
            String purpose,
            String requestedModel,
            String provider,
            String model,
            String upstreamModel,
            boolean stream,
            boolean success,
            boolean timeout,
            String errorMessage,
            long latencyMs,
            Long ttftMs,
            Long tpotMs,
            long promptTokens,
            long completionTokens,
            long totalTokens,
            BigDecimal tokensPerSecond,
            BigDecimal cost,
            BigDecimal costPerToken
    ) {
    }

    /** 对一组性能事实进行增量汇总的统计对象。 */
    public static class Summary {
        private long requests;
        private long successes;
        private long errors;
        private long timeouts;
        private long latencyMsTotal;
        private long ttftMsTotal;
        private long ttftSamples;
        private long tpotMsTotal;
        private long tpotSamples;
        private long totalTokens;
        private BigDecimal cost = BigDecimal.ZERO;

        /** 累加一条性能记录到当前聚合器。 */
        void add(PerformanceRecord record) {
            requests++;
            if (record.success()) {
                successes++;
            } else {
                errors++;
            }
            if (record.timeout()) {
                timeouts++;
            }
            latencyMsTotal += record.latencyMs();
            if (record.ttftMs() != null) {
                ttftMsTotal += record.ttftMs();
                ttftSamples++;
            }
            if (record.tpotMs() != null) {
                tpotMsTotal += record.tpotMs();
                tpotSamples++;
            }
            totalTokens += record.totalTokens();
            cost = cost.add(record.cost());
        }

        /** 返回聚合调用总数。 */
        public long getRequests() {
            return requests;
        }

        /** 返回成功调用数量。 */
        public long getSuccesses() {
            return successes;
        }

        /** 返回失败调用数量。 */
        public long getErrors() {
            return errors;
        }

        /** 返回失败调用占总调用的比例。 */
        public BigDecimal getErrorRate() {
            return rate(errors);
        }

        /** 返回被归类为超时的失败数量。 */
        public long getTimeouts() {
            return timeouts;
        }

        /** 返回超时调用占总调用的比例。 */
        public BigDecimal getTimeoutRate() {
            return rate(timeouts);
        }

        /** 返回全量调用的平均端到端时延毫秒数。 */
        public long getAvgLatencyMs() {
            return requests == 0 ? 0 : latencyMsTotal / requests;
        }

        /** 返回具有首 Token 数据样本的平均首 Token 时延。 */
        public long getAvgTtftMs() {
            return ttftSamples == 0 ? 0 : ttftMsTotal / ttftSamples;
        }

        /** 返回具有 Token 间隔数据样本的平均生成间隔。 */
        public long getAvgTpotMs() {
            return tpotSamples == 0 ? 0 : tpotMsTotal / tpotSamples;
        }

        /** 以累计请求时延估算成功调用吞吐率，供趋势观察而非容量承诺。 */
        public BigDecimal getQps() {
            if (latencyMsTotal <= 0) {
                return BigDecimal.ZERO;
            }
            return BigDecimal.valueOf(successes).multiply(BigDecimal.valueOf(1000))
                    .divide(BigDecimal.valueOf(latencyMsTotal), 4, RoundingMode.HALF_UP);
        }

        /** 以累计时延计算平均 Token 吞吐率。 */
        public BigDecimal getTokensPerSecond() {
            if (latencyMsTotal <= 0) {
                return BigDecimal.ZERO;
            }
            return BigDecimal.valueOf(totalTokens).multiply(BigDecimal.valueOf(1000))
                    .divide(BigDecimal.valueOf(latencyMsTotal), 4, RoundingMode.HALF_UP);
        }

        /** 返回聚合成本。 */
        public BigDecimal getCost() {
            return cost;
        }

        /** 返回每次调用的平均成本，空集合时安全返回零。 */
        public BigDecimal getCostPerRequest() {
            return requests == 0 ? BigDecimal.ZERO : cost.divide(BigDecimal.valueOf(requests), 8, RoundingMode.HALF_UP);
        }

        /** 将计数转换为相对总请求数的比例，避免除零。 */
        private BigDecimal rate(long numerator) {
            return requests == 0 ? BigDecimal.ZERO : BigDecimal.valueOf(numerator)
                    .divide(BigDecimal.valueOf(requests), 4, RoundingMode.HALF_UP);
        }
    }
}
