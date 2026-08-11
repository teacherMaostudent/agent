package com.zxf.ai.gateway.governance;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class PromptGovernanceService {
    /**
     * 三类治理对象分别保存：输出契约、后处理链、Prompt 实验。
     * 这对应面试里的“Prompt、参数、后处理、流程约束分别怎么治理”。
     */
    private static final String CONTRACT_KIND = "output-contract";
    private static final String POST_PROCESS_KIND = "post-process-chain";
    private static final String EXPERIMENT_KIND = "prompt-experiment";

    private final RuntimeStateRepository stateRepository;
    private final ObjectMapper objectMapper;
    private final Map<String, OutputContract> contracts = new LinkedHashMap<>();
    private final Map<String, PostProcessChain> chains = new LinkedHashMap<>();
    private final List<PromptExperiment> experiments = new ArrayList<>();

    /** 注入可选治理状态仓储与 JSON 工具，使内存演示和持久化部署使用同一服务语义。 */
    public PromptGovernanceService(ObjectProvider<RuntimeStateRepository> stateRepository, ObjectMapper objectMapper) {
        this.stateRepository = stateRepository.getIfAvailable();
        this.objectMapper = objectMapper;
    }

    /**
     * 新增或更新输出契约。
     *
     * <p>输出契约不直接替代 JSON Schema 引擎，而是用轻量 requiredFields 先把
     * “必须有哪些字段”固化下来，适合面试项目演示结构化输出治理。</p>
     */
    public synchronized OutputContract upsertContract(OutputContract request) {
        String id = id(request.id());
        OutputContract saved = new OutputContract(
                id,
                request.name(),
                request.description(),
                request.requiredFields() == null ? List.of() : request.requiredFields(),
                request.schemaExample() == null ? Map.of() : request.schemaExample(),
                Instant.now()
        );
        save(CONTRACT_KIND, id, saved, contracts);
        return saved;
    }

    /**
     * 按输出契约校验模型输出。
     *
     * <p>如果模型没有输出 requiredFields，就能把问题归因到 Prompt 约束不足、
     * 模型参数不合适或后处理链缺失。</p>
     */
    public synchronized ValidationResult validate(String contractId, JsonNode output) {
        OutputContract contract = find(CONTRACT_KIND, contractId, OutputContract.class, contracts);
        List<String> errors = new ArrayList<>();
        for (String field : contract.requiredFields()) {
            if (!output.hasNonNull(field)) {
                errors.add("Missing required field: " + field);
            }
        }
        return new ValidationResult(contractId, errors.isEmpty(), errors, output);
    }

    /**
     * 新增或更新后处理链。
     *
     * <p>后处理链用于记录确定性规则，例如去掉 markdown fence、解析 JSON、
     * 风险等级归一化、高风险强制人审等。</p>
     */
    public synchronized PostProcessChain upsertPostProcessChain(PostProcessChain request) {
        String id = id(request.id());
        PostProcessChain saved = new PostProcessChain(
                id,
                request.name(),
                request.steps() == null ? List.of() : request.steps(),
                Instant.now()
        );
        save(POST_PROCESS_KIND, id, saved, chains);
        return saved;
    }

    /**
     * 记录一次 Prompt 实验。
     *
     * <p>用于回答“你解决 bad case 后有没有评测数据证明有效”：这里保存修复前输出、
     * 修复后输出、参数变化和指标摘要。</p>
     */
    public synchronized PromptExperiment recordExperiment(PromptExperimentRequest request) {
        PromptExperiment saved = new PromptExperiment(
                UUID.randomUUID().toString(),
                Instant.now(),
                request.promptVersionId(),
                request.model(),
                request.parameters() == null ? Map.of() : request.parameters(),
                request.badCaseId(),
                request.beforeOutput(),
                request.afterOutput(),
                request.metricSummary() == null ? Map.of() : request.metricSummary(),
                request.conclusion()
        );
        if (stateRepository != null) {
            stateRepository.saveDocument(EXPERIMENT_KIND, saved.id(), saved);
        } else {
            experiments.add(saved);
        }
        return saved;
    }

    /**
     * 管理端快照，展示所有 Prompt 治理资产。
     */
    public synchronized Map<String, Object> snapshot() {
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "outputContracts", list(CONTRACT_KIND, OutputContract.class, contracts),
                "postProcessChains", list(POST_PROCESS_KIND, PostProcessChain.class, chains),
                "promptExperiments", stateRepository == null ? List.copyOf(experiments) : stateRepository.listDocuments(EXPERIMENT_KIND, PromptExperiment.class),
                "interviewNotes", List.of(
                        "Prompt contract fixes output shape with required fields.",
                        "Post-process chain records deterministic constraints after model generation.",
                        "Prompt experiment stores before/after output and metric evidence for bad case repair."
                )
        );
    }

    /**
     * 按当前模式保存对象：有 MySQL 写库，没有 MySQL 写内存。
     */
    private <T> void save(String kind, String id, T value, Map<String, T> memory) {
        if (stateRepository != null) {
            stateRepository.saveDocument(kind, id, value);
        } else {
            memory.put(id, value);
        }
    }

    /**
     * 查找指定治理对象，主要供 validate 使用。
     */
    private <T> T find(String kind, String id, Class<T> type, Map<String, T> memory) {
        if (stateRepository != null) {
            return stateRepository.findDocument(kind, id, type)
                    .orElseThrow(() -> new IllegalArgumentException("Unknown " + kind + ": " + id));
        }
        T value = memory.get(id);
        if (value == null) {
            throw new IllegalArgumentException("Unknown " + kind + ": " + id);
        }
        return value;
    }

    /**
     * 列出指定 kind 的治理对象。
     */
    private <T> List<T> list(String kind, Class<T> type, Map<String, T> memory) {
        return stateRepository == null ? List.copyOf(memory.values()) : stateRepository.listDocuments(kind, type);
    }

    /**
     * 如果调用方没有传 id，就自动生成一个，方便 HTTP 示例直接运行。
     */
    private String id(String id) {
        return id == null || id.isBlank() ? UUID.randomUUID().toString() : id;
    }

    /** 描述模型输出必须满足的字段契约及示例结构。 */
    public record OutputContract(String id, String name, String description, List<String> requiredFields, Map<String, Object> schemaExample, Instant updatedAt) {
    }

    /** 封装待按输出契约验证的结构化模型结果。 */
    public record ValidationRequest(String contractId, JsonNode output) {
    }

    /** 表达输出契约校验结论及缺失字段清单。 */
    public record ValidationResult(String contractId, boolean valid, List<String> errors, JsonNode output) {
    }

    /** 表达发布后须执行的确定性后处理步骤集合。 */
    public record PostProcessChain(String id, String name, List<String> steps, Instant updatedAt) {
    }

    /** 封装 Prompt 变更实验的输入、对照输出和指标摘要。 */
    public record PromptExperimentRequest(
            String promptVersionId,
            String model,
            Map<String, Object> parameters,
            String badCaseId,
            String beforeOutput,
            String afterOutput,
            Map<String, Object> metricSummary,
            String conclusion
    ) {
    }

    /** 保存可追溯的 Prompt 实验事实，供质量回归和审计复查。 */
    public record PromptExperiment(
            String id,
            Instant createdAt,
            String promptVersionId,
            String model,
            Map<String, Object> parameters,
            String badCaseId,
            String beforeOutput,
            String afterOutput,
            Map<String, Object> metricSummary,
            String conclusion
    ) {
    }
}
