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

    public ModelPerformanceService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

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

    /** Metrics used by the release controller, scoped to one physical provider:model target. */
    public Summary summarizeSince(Instant since, String requestedModel, String provider, String model) {
        return summarize(snapshot().stream()
                .filter(record -> !record.timestamp().isBefore(since))
                .filter(record -> requestedModel.equals(record.requestedModel()))
                .filter(record -> provider.equals(record.provider()) && model.equals(record.model()))
                .toList());
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

    private void record(PerformanceRecord record) {
        if (stateRepository != null) {
            stateRepository.saveDocument(KIND, record.requestId() + "-" + UUID.randomUUID(), record);
            return;
        }
        synchronized (records) {
            records.add(record);
        }
    }

    private List<PerformanceRecord> snapshot() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(KIND, PerformanceRecord.class);
        }
        synchronized (records) {
            return List.copyOf(records);
        }
    }

    private Map<String, Summary> group(List<PerformanceRecord> source, java.util.function.Function<PerformanceRecord, String> classifier) {
        Map<String, Summary> result = new LinkedHashMap<>();
        for (PerformanceRecord record : source) {
            result.computeIfAbsent(classifier.apply(record), ignored -> new Summary()).add(record);
        }
        return result;
    }

    private Summary summarize(List<PerformanceRecord> source) {
        Summary summary = new Summary();
        source.forEach(summary::add);
        return summary;
    }

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

        public long getRequests() {
            return requests;
        }

        public long getSuccesses() {
            return successes;
        }

        public long getErrors() {
            return errors;
        }

        public BigDecimal getErrorRate() {
            return rate(errors);
        }

        public long getTimeouts() {
            return timeouts;
        }

        public BigDecimal getTimeoutRate() {
            return rate(timeouts);
        }

        public long getAvgLatencyMs() {
            return requests == 0 ? 0 : latencyMsTotal / requests;
        }

        public long getAvgTtftMs() {
            return ttftSamples == 0 ? 0 : ttftMsTotal / ttftSamples;
        }

        public long getAvgTpotMs() {
            return tpotSamples == 0 ? 0 : tpotMsTotal / tpotSamples;
        }

        public BigDecimal getQps() {
            if (latencyMsTotal <= 0) {
                return BigDecimal.ZERO;
            }
            return BigDecimal.valueOf(successes).multiply(BigDecimal.valueOf(1000))
                    .divide(BigDecimal.valueOf(latencyMsTotal), 4, RoundingMode.HALF_UP);
        }

        public BigDecimal getTokensPerSecond() {
            if (latencyMsTotal <= 0) {
                return BigDecimal.ZERO;
            }
            return BigDecimal.valueOf(totalTokens).multiply(BigDecimal.valueOf(1000))
                    .divide(BigDecimal.valueOf(latencyMsTotal), 4, RoundingMode.HALF_UP);
        }

        public BigDecimal getCost() {
            return cost;
        }

        public BigDecimal getCostPerRequest() {
            return requests == 0 ? BigDecimal.ZERO : cost.divide(BigDecimal.valueOf(requests), 8, RoundingMode.HALF_UP);
        }

        private BigDecimal rate(long numerator) {
            return requests == 0 ? BigDecimal.ZERO : BigDecimal.valueOf(numerator)
                    .divide(BigDecimal.valueOf(requests), 4, RoundingMode.HALF_UP);
        }
    }
}
