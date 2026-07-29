package com.zxf.ai.gateway.badcase;

import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class BadCaseService {
    /**
     * Bad Case 使用统一运行状态表保存，kind 固定为 bad-case。
     * 这样不需要为了学习项目拆很多业务表，但仍然能做到重启后可追溯。
     */
    private static final String KIND = "bad-case";

    private final RuntimeStateRepository stateRepository;
    private final List<BadCaseRecord> memory = new ArrayList<>();

    public BadCaseService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

    /**
     * 新建一条模型不听指令的复盘记录。
     *
     * <p>面试里被问“bad case 怎么定位”时，重点不是只说调 Prompt，而是要说明：
     * 保存原 Prompt、模型参数、原始输出、期望输出、定位方法和修复证据。</p>
     */
    public synchronized BadCaseRecord create(BadCaseRequest request) {
        BadCaseRecord record = new BadCaseRecord(
                UUID.randomUUID().toString(),
                Instant.now(),
                request.title(),
                firstNonBlank(request.model(), "unknown"),
                firstNonBlank(request.prompt(), ""),
                request.modelParameters() == null ? Map.of() : request.modelParameters(),
                firstNonBlank(request.modelOutput(), ""),
                firstNonBlank(request.expectedOutput(), ""),
                firstNonBlank(request.locationMethod(), "prompt/model-params/output-diff/manual-review"),
                firstNonBlank(request.rootCause(), "UNKNOWN"),
                firstNonBlank(request.fixStrategy(), ""),
                firstNonBlank(request.evaluationEvidence(), ""),
                "OPEN",
                firstNonBlank(request.owner(), "unassigned"),
                request.tags() == null ? List.of() : request.tags()
        );
        save(record);
        return record;
    }

    /**
     * 将 bad case 从 OPEN 标记为 RESOLVED。
     *
     * <p>这里允许补充根因、修复策略和评测证据，形成“发现问题 -> 定位问题 -> 修复问题 ->
     * 用指标证明有效”的闭环。</p>
     */
    public synchronized BadCaseRecord resolve(String id, ResolveBadCaseRequest request) {
        BadCaseRecord current = find(id);
        BadCaseRecord resolved = new BadCaseRecord(
                current.id(),
                current.createdAt(),
                current.title(),
                current.model(),
                current.prompt(),
                current.modelParameters(),
                current.modelOutput(),
                current.expectedOutput(),
                current.locationMethod(),
                firstNonBlank(request.rootCause(), current.rootCause()),
                firstNonBlank(request.fixStrategy(), current.fixStrategy()),
                firstNonBlank(request.evaluationEvidence(), current.evaluationEvidence()),
                "RESOLVED",
                firstNonBlank(request.owner(), current.owner()),
                current.tags()
        );
        save(resolved);
        return resolved;
    }

    /**
     * 返回全部 bad case。
     *
     * <p>如果启用了 MySQL 持久化，从 llm_runtime_documents 读取；否则使用内存列表兜底。</p>
     */
    public synchronized List<BadCaseRecord> list() {
        if (stateRepository != null) {
            return stateRepository.listDocuments(KIND, BadCaseRecord.class);
        }
        return List.copyOf(memory);
    }

    /**
     * 按 id 查找 bad case，供 resolve 使用。
     */
    private BadCaseRecord find(String id) {
        if (stateRepository != null) {
            return stateRepository.findDocument(KIND, id, BadCaseRecord.class)
                    .orElseThrow(() -> new IllegalArgumentException("Unknown bad case: " + id));
        }
        return memory.stream()
                .filter(item -> item.id().equals(id))
                .findFirst()
                .orElseThrow(() -> new IllegalArgumentException("Unknown bad case: " + id));
    }

    /**
     * 统一保存入口，屏蔽“内存模式”和“MySQL 模式”的差异。
     */
    private void save(BadCaseRecord record) {
        if (stateRepository != null) {
            stateRepository.saveDocument(KIND, record.id(), record);
            return;
        }
        memory.removeIf(item -> item.id().equals(record.id()));
        memory.add(record);
    }

    /**
     * 防止接口调用方传空字符串导致记录不可读。
     */
    private String firstNonBlank(String first, String fallback) {
        return first == null || first.isBlank() ? fallback : first;
    }

    public record BadCaseRequest(
            String title,
            String model,
            String prompt,
            Map<String, Object> modelParameters,
            String modelOutput,
            String expectedOutput,
            String locationMethod,
            String rootCause,
            String fixStrategy,
            String evaluationEvidence,
            String owner,
            List<String> tags
    ) {
    }

    public record ResolveBadCaseRequest(String rootCause, String fixStrategy, String evaluationEvidence, String owner) {
    }

    public record BadCaseRecord(
            String id,
            Instant createdAt,
            String title,
            String model,
            String prompt,
            Map<String, Object> modelParameters,
            String modelOutput,
            String expectedOutput,
            String locationMethod,
            String rootCause,
            String fixStrategy,
            String evaluationEvidence,
            String status,
            String owner,
            List<String> tags
    ) {
    }
}
