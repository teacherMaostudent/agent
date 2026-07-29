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

    public UsageReportService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

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

    public void clear() {
        if (stateRepository != null) {
            stateRepository.deleteKind(KIND);
            return;
        }
        synchronized (records) {
            records.clear();
        }
    }

    private List<UsageRecord> snapshot() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(KIND, UsageRecord.class);
        }
        synchronized (records) {
            return List.copyOf(records);
        }
    }

    private Map<String, Summary> group(List<UsageRecord> source, java.util.function.Function<UsageRecord, String> classifier) {
        Map<String, Summary> result = new LinkedHashMap<>();
        for (UsageRecord record : source) {
            result.computeIfAbsent(classifier.apply(record), ignored -> new Summary()).add(record);
        }
        return result;
    }

    private Summary summarize(List<UsageRecord> source) {
        Summary summary = new Summary();
        source.forEach(summary::add);
        return summary;
    }

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

    public static class Summary {
        private long requests;
        private long promptTokens;
        private long completionTokens;
        private long totalTokens;
        private BigDecimal cost = BigDecimal.ZERO;
        private long latencyMsTotal;
        private long absolutePredictionErrorTotal;
        private long predictions;

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

        public long getRequests() {
            return requests;
        }

        public long getPromptTokens() {
            return promptTokens;
        }

        public long getCompletionTokens() {
            return completionTokens;
        }

        public long getTotalTokens() {
            return totalTokens;
        }

        public BigDecimal getCost() {
            return cost;
        }

        public long getAvgLatencyMs() {
            return requests == 0 ? 0 : latencyMsTotal / requests;
        }

        public long getAvgAbsolutePredictionErrorTokens() {
            return predictions == 0 ? 0 : absolutePredictionErrorTotal / predictions;
        }
    }
}
