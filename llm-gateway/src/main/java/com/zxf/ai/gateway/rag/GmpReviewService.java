package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import com.zxf.ai.gateway.report.UsageReportService;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Mono;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
@ConditionalOnProperty(prefix = "rag-agent", name = "enabled", havingValue = "true")
public class GmpReviewService {
    private static final String TASK_KIND = "gmp-review-task";
    private static final String AUDIT_KIND = "gmp-review-audit";

    private final RagAgentClient ragAgentClient;
    private final RuntimeStateRepository stateRepository;
    private final UsageReportService usageReportService;
    private final Map<String, GmpReviewTask> tasks = new LinkedHashMap<>();
    private final List<GmpAuditEntry> audits = new ArrayList<>();

    public GmpReviewService(
            RagAgentClient ragAgentClient,
            ObjectProvider<RuntimeStateRepository> stateRepository,
            UsageReportService usageReportService
    ) {
        this.ragAgentClient = ragAgentClient;
        this.stateRepository = stateRepository.getIfAvailable();
        this.usageReportService = usageReportService;
    }

    public Mono<JsonNode> uploadDocument(FilePart file, String businessId, String documentType, String tenantId, String userId) {
        long started = System.nanoTime();
        return ragAgentClient.uploadDocument(file, businessId, documentType)
                .doOnNext(response -> {
                    BigDecimal cost = extractCost(response);
                    long latencyMs = elapsedMs(started);
                    usageReportService.recordExternal(UUID.randomUUID().toString(), tenantId, userId,
                            "document-upload", "gmp-document-upload", cost, latencyMs);
                    audit(null, userId, "DOCUMENT_UPLOADED", "UPLOADED", "", cost, latencyMs, response,
                            "businessId=" + empty(businessId) + ", documentType=" + empty(documentType));
                });
    }

    public Mono<GmpReviewTask> startReview(GmpReviewRequest request, String tenantId, String userId) {
        String taskId = request.taskId() == null || request.taskId().isBlank() ? UUID.randomUUID().toString() : request.taskId();
        Instant now = Instant.now();
        GmpReviewTask created = new GmpReviewTask(
                taskId,
                null,
                request.documentId(),
                request.businessId(),
                request.documentType(),
                tenantId,
                userId,
                "REVIEWING",
                "",
                "rag-agent-service review is running.",
                false,
                BigDecimal.ZERO,
                0,
                null,
                null,
                null,
                "",
                request.metadata() == null ? Map.of() : request.metadata(),
                now,
                now
        );
        save(created);
        audit(taskId, userId, "TASK_CREATED", "REVIEWING", "", BigDecimal.ZERO, 0, null, "Forwarded to rag-agent-service.");

        long started = System.nanoTime();
        GmpReviewRequest forwarded = new GmpReviewRequest(
                taskId,
                request.documentId(),
                request.businessId(),
                request.documentType(),
                request.content(),
                request.model(),
                request.checklistVersion(),
                request.reviewerHint(),
                request.metadata()
        );
        return ragAgentClient.startGmpReview(forwarded)
                .map(response -> completeFromRagResponse(created, response, elapsedMs(started), userId));
    }

    public Mono<GmpReviewTask> refresh(String taskId) {
        GmpReviewTask current = task(taskId);
        if (current.ragReviewId() == null || current.ragReviewId().isBlank()) {
            return Mono.just(current);
        }
        long started = System.nanoTime();
        return ragAgentClient.getReview(current.ragReviewId())
                .map(response -> completeFromRagResponse(current, response, elapsedMs(started), current.userId()));
    }

    public Mono<GmpReviewTask> rerun(String taskId) {
        GmpReviewTask current = task(taskId);
        if (current.ragReviewId() == null || current.ragReviewId().isBlank()) {
            return startReview(new GmpReviewRequest(
                    current.taskId(),
                    current.documentId(),
                    current.businessId(),
                    current.documentType(),
                    null,
                    null,
                    null,
                    "rerun from llm-gateway",
                    current.metadata()
            ), current.tenantId(), current.userId());
        }
        long started = System.nanoTime();
        return ragAgentClient.rerunReview(current.ragReviewId())
                .map(response -> completeFromRagResponse(current, response, elapsedMs(started), current.userId()));
    }

    public synchronized GmpReviewTask confirm(String taskId, GmpHumanReviewRequest request) {
        GmpReviewTask current = task(taskId);
        String action = firstNonBlank(request.action(), "CONFIRMED").toUpperCase();
        String status = switch (action) {
            case "REJECT", "REJECTED" -> "REJECTED";
            case "APPROVE", "APPROVED", "CONFIRM", "CONFIRMED" -> "APPROVED";
            default -> "NEED_HUMAN_REVIEW";
        };
        Instant now = Instant.now();
        GmpReviewTask updated = new GmpReviewTask(
                current.taskId(),
                current.ragReviewId(),
                current.documentId(),
                current.businessId(),
                current.documentType(),
                current.tenantId(),
                current.userId(),
                status,
                firstNonBlank(request.finalRiskLevel(), current.riskLevel()),
                firstNonBlank(request.finalSummary(), current.summary()),
                false,
                current.cost(),
                current.latencyMs(),
                request.finalResult() == null ? current.ragResponse() : request.finalResult(),
                firstNonBlank(request.reviewer(), current.userId()),
                now,
                firstNonBlank(request.notes(), ""),
                current.metadata(),
                current.createdAt(),
                now
        );
        save(updated);
        audit(taskId, updated.reviewer(), "HUMAN_" + status, status, updated.riskLevel(), updated.cost(),
                updated.latencyMs(), updated.ragResponse(), updated.reviewNotes());
        return updated;
    }

    public synchronized List<GmpReviewTask> tasks() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(TASK_KIND, GmpReviewTask.class).stream()
                    .sorted(Comparator.comparing(GmpReviewTask::createdAt).reversed())
                    .toList();
        }
        return tasks.values().stream()
                .sorted(Comparator.comparing(GmpReviewTask::createdAt).reversed())
                .toList();
    }

    public synchronized GmpReviewTask task(String taskId) {
        if (stateRepository != null) {
            return stateRepository.findDocument(TASK_KIND, taskId, GmpReviewTask.class)
                    .orElseThrow(() -> new GatewayException(HttpStatus.NOT_FOUND, "Unknown GMP review task: " + taskId));
        }
        GmpReviewTask task = tasks.get(taskId);
        if (task == null) {
            throw new GatewayException(HttpStatus.NOT_FOUND, "Unknown GMP review task: " + taskId);
        }
        return task;
    }

    public synchronized Map<String, Object> snapshot() {
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql-runtime-documents",
                "tasks", tasks().stream().limit(100).toList(),
                "auditLogs", auditLogs()
        );
    }

    public synchronized List<GmpAuditEntry> auditLogs() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(AUDIT_KIND, GmpAuditEntry.class).stream()
                    .sorted(Comparator.comparing(GmpAuditEntry::timestamp).reversed())
                    .limit(200)
                    .toList();
        }
        return audits.stream()
                .sorted(Comparator.comparing(GmpAuditEntry::timestamp).reversed())
                .limit(200)
                .toList();
    }

    private GmpReviewTask completeFromRagResponse(GmpReviewTask current, JsonNode response, long latencyMs, String actor) {
        BigDecimal cost = extractCost(response);
        String status = extractStatus(response);
        String riskLevel = firstNonBlank(text(response, "riskLevel"), text(response, "risk_level"));
        String summary = firstNonBlank(text(response, "summary"), text(response, "message"));
        boolean needHumanReview = response.path("needHumanReview").asBoolean(response.path("need_human_review").asBoolean(isHighRisk(riskLevel)));
        String ragReviewId = firstNonBlank(text(response, "reviewId"), firstNonBlank(text(response, "review_id"), text(response, "id")));
        GmpReviewTask updated = new GmpReviewTask(
                current.taskId(),
                firstNonBlank(ragReviewId, current.ragReviewId()),
                current.documentId(),
                current.businessId(),
                current.documentType(),
                current.tenantId(),
                current.userId(),
                needHumanReview && "DONE".equals(status) ? "NEED_HUMAN_REVIEW" : status,
                firstNonBlank(riskLevel, current.riskLevel()),
                firstNonBlank(summary, current.summary()),
                needHumanReview,
                cost,
                latencyMs,
                response,
                current.reviewer(),
                current.reviewedAt(),
                current.reviewNotes(),
                current.metadata(),
                current.createdAt(),
                Instant.now()
        );
        save(updated);
        usageReportService.recordExternal(updated.taskId(), updated.tenantId(), updated.userId(),
                "gmp-review", "rag-agent-gmp-review", cost, latencyMs);
        audit(updated.taskId(), actor, "RAG_REVIEW_UPDATED", updated.status(), updated.riskLevel(), cost, latencyMs,
                response, "ragReviewId=" + empty(updated.ragReviewId()));
        return updated;
    }

    private String extractStatus(JsonNode response) {
        String status = firstNonBlank(text(response, "status"), text(response, "state")).toUpperCase();
        if (status.isBlank() || "SUCCESS".equals(status) || "COMPLETED".equals(status)) {
            return "DONE";
        }
        return status;
    }

    private BigDecimal extractCost(JsonNode response) {
        JsonNode cost = response.path("cost");
        if (cost.isNumber()) {
            return cost.decimalValue();
        }
        for (String field : List.of("total", "totalCost", "total_cost", "costEstimated", "cost_estimated")) {
            JsonNode value = cost.path(field);
            if (value.isNumber()) {
                return value.decimalValue();
            }
        }
        JsonNode rootCost = response.path("costEstimated");
        return rootCost.isNumber() ? rootCost.decimalValue() : BigDecimal.ZERO;
    }

    private boolean isHighRisk(String riskLevel) {
        String normalized = riskLevel == null ? "" : riskLevel.toUpperCase();
        return "HIGH".equals(normalized) || "CRITICAL".equals(normalized);
    }

    private String text(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isTextual() ? value.asText() : "";
    }

    private synchronized void save(GmpReviewTask task) {
        if (stateRepository != null) {
            stateRepository.saveDocument(TASK_KIND, task.taskId(), task);
            stateRepository.saveGmpReviewTask(task);
        } else {
            tasks.put(task.taskId(), task);
        }
    }

    private synchronized void audit(String taskId, String actor, String action, String status, String riskLevel,
                                    BigDecimal cost, long latencyMs, JsonNode payload, String notes) {
        GmpAuditEntry entry = new GmpAuditEntry(
                UUID.randomUUID().toString(),
                taskId,
                Instant.now(),
                firstNonBlank(actor, "system"),
                action,
                status,
                riskLevel == null ? "" : riskLevel,
                cost == null ? BigDecimal.ZERO : cost,
                latencyMs,
                payload,
                notes == null ? "" : notes
        );
        if (stateRepository != null) {
            stateRepository.saveDocument(AUDIT_KIND, entry.id(), entry);
        } else {
            audits.add(entry);
        }
    }

    private long elapsedMs(long startedNanos) {
        return (System.nanoTime() - startedNanos) / 1_000_000;
    }

    private String firstNonBlank(String first, String second) {
        return first == null || first.isBlank() ? (second == null ? "" : second) : first;
    }

    private String empty(String value) {
        return value == null ? "" : value;
    }
}
