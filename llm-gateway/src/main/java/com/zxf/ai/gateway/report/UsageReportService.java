package com.zxf.ai.gateway.report;

import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.GatewayUsage;
import com.zxf.ai.gateway.model.ModelEndpoint;
import com.zxf.ai.gateway.usage.OutputTokenPrediction;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Component
public class UsageReportService {
    private static final String KIND = "usage-record";

    private final RuntimeStateRepository stateRepository;
    private final List<UsageRecord> records = new ArrayList<>();

    /** 选择可选持久化仓储；无仓储时仅保留当前实例的用量数据。 */
    public UsageReportService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

    /** 记录一次网关模型调用的 Token、成本和预测偏差输入，保留完整关联标识。 */
    public void record(GatewayRequestContext context, ModelEndpoint endpoint, GatewayUsage usage,
                       OutputTokenPrediction prediction, long latencyMs) {
        UsageRecord record = new UsageRecord(
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
                context.costBudget(),
                endpoint.providerName(),
                endpoint.modelName(),
                endpoint.upstreamModel(),
                context.stream(),
                usage.promptTokens(),
                usage.completionTokens(),
                usage.totalTokens(),
                usage.cost(),
                usage.currency(),
                usage.usageSource(),
                usage.costStatus(),
                prediction == null ? 0 : prediction.selected(),
                prediction == null ? 0 : prediction.p95(),
                prediction == null ? "" : prediction.modelVersion(),
                prediction == null ? 0 : prediction.sampleCount(),
                latencyMs
        );
        if (stateRepository != null) {
            stateRepository.saveDocument(KIND, record.requestId() + "-" + UUID.randomUUID(), record);
            return;
        }
        synchronized (records) {
            records.add(record);
        }
    }

    /** 接收其他平台组件报告的成本，明确标记其非网关本地 Token 计量来源。 */
    public void recordExternal(String requestId, String tenantId, String userId, String component,
                               String operation, BigDecimal cost, long latencyMs) {
        UsageRecord record = new UsageRecord(
                Instant.now(),
                requestId,
                requestId,
                tenantId,
                userId,
                "external-service",
                "unversioned",
                "stateless",
                requestId,
                operation,
                null,
                "rag-agent",
                operation,
                component,
                false,
                0,
                0,
                0,
                cost == null ? BigDecimal.ZERO : cost,
                "",
                "EXTERNAL_REPORTED",
                "REPORTED",
                0,
                0,
                "external",
                0,
                latencyMs
        );
        if (stateRepository != null) {
            stateRepository.saveDocument(KIND, record.requestId() + "-" + UUID.randomUUID(), record);
            return;
        }
        synchronized (records) {
            records.add(record);
        }
    }

    /** 生成全量用量与成本汇总，并按提供商、模型、用户和租户分组。 */
    public Map<String, Object> report() {
        List<UsageRecord> snapshot = snapshot();
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "total", summarize(snapshot),
                "byProvider", group(snapshot, UsageRecord::provider),
                "byModel", group(snapshot, UsageRecord::model),
                "byUser", group(snapshot, UsageRecord::userId),
                "byTenant", group(snapshot, UsageRecord::tenantId),
                "recent", snapshot.stream()
                        .sorted(Comparator.comparing(UsageRecord::timestamp).reversed())
                        .limit(50)
                        .toList()
        );
    }

    /** 生成当天用量与成本快照，作为日级预算和异常排查输入。 */
    public Map<String, Object> dailyReport() {
        LocalDate today = LocalDate.now();
        List<UsageRecord> todayRecords = snapshot().stream()
                .filter(record -> LocalDate.ofInstant(record.timestamp(), ZoneId.systemDefault()).equals(today))
                .toList();
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "date", today.toString(),
                "total", summarize(todayRecords),
                "byProvider", group(todayRecords, UsageRecord::provider),
                "byModel", group(todayRecords, UsageRecord::model),
                "byUser", group(todayRecords, UsageRecord::userId)
        );
    }

    /** 清空用量历史；持久化部署将删除同种类记录，必须由授权管理操作触发。 */
    public void clear() {
        if (stateRepository != null) {
            stateRepository.deleteKind(KIND);
            return;
        }
        synchronized (records) {
            records.clear();
        }
    }

    /** 返回稳定的用量记录副本，隔离报表读取与并发写入。 */
    private List<UsageRecord> snapshot() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(KIND, UsageRecord.class);
        }
        synchronized (records) {
            return List.copyOf(records);
        }
    }

    /** 按调用方给定维度聚合用量记录。 */
    private Map<String, Summary> group(List<UsageRecord> source, java.util.function.Function<UsageRecord, String> classifier) {
        Map<String, Summary> result = new LinkedHashMap<>();
        for (UsageRecord record : source) {
            result.computeIfAbsent(classifier.apply(record), ignored -> new Summary()).add(record);
        }
        return result;
    }

    /** 将一组用量记录归并为总量、成本和预测误差统计。 */
    private Summary summarize(List<UsageRecord> source) {
        Summary summary = new Summary();
        source.forEach(summary::add);
        return summary;
    }

    /** 保存单次模型或外部组件成本上报的不可变用量事实。 */
    public record UsageRecord(
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
            BigDecimal costBudget,
            String provider,
            String model,
            String upstreamModel,
            boolean stream,
            long promptTokens,
            long completionTokens,
            long totalTokens,
            BigDecimal cost,
            String currency,
            String usageSource,
            String costStatus,
            long predictedCompletionTokens,
            long predictedP95Tokens,
            String predictionModelVersion,
            long predictionSampleCount,
            long latencyMs
    ) {
    }

    /** 对用量、成本和 Token 预测误差进行增量聚合的统计对象。 */
    public static class Summary {
        private long requests;
        private long promptTokens;
        private long completionTokens;
        private long totalTokens;
        private BigDecimal cost = BigDecimal.ZERO;
        private long latencyMsTotal;
        private long absolutePredictionErrorTotal;
        private long predictions;

        /** 将一条用量记录累加到当前汇总，并仅对有预测的记录计算误差。 */
        void add(UsageRecord record) {
            requests++;
            promptTokens += record.promptTokens();
            completionTokens += record.completionTokens();
            totalTokens += record.totalTokens();
            cost = cost.add(record.cost());
            latencyMsTotal += record.latencyMs();
            if (record.predictedCompletionTokens() > 0) {
                predictions++;
                absolutePredictionErrorTotal += Math.abs(record.completionTokens() - record.predictedCompletionTokens());
            }
        }

        /** 返回汇总中的调用次数。 */
        public long getRequests() {
            return requests;
        }

        /** 返回汇总提示词 Token 数。 */
        public long getPromptTokens() {
            return promptTokens;
        }

        /** 返回汇总生成 Token 数。 */
        public long getCompletionTokens() {
            return completionTokens;
        }

        /** 返回汇总总 Token 数。 */
        public long getTotalTokens() {
            return totalTokens;
        }

        /** 返回汇总成本。 */
        public BigDecimal getCost() {
            return cost;
        }

        /** 返回调用平均时延毫秒数。 */
        public long getAvgLatencyMs() {
            return requests == 0 ? 0 : latencyMsTotal / requests;
        }

        /** 返回有预测样本的平均绝对生成 Token 误差。 */
        public long getAvgAbsolutePredictionErrorTokens() {
            return predictions == 0 ? 0 : absolutePredictionErrorTotal / predictions;
        }
    }
}
