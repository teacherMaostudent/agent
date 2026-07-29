package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.badcase.BadCaseService;
import com.zxf.ai.gateway.governance.PromptGovernanceService;
import com.zxf.ai.gateway.redis.RedisStructureDemoService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
public class EngineeringGovernanceController {
    private final BadCaseService badCaseService;
    private final PromptGovernanceService promptGovernanceService;
    private final RedisStructureDemoService redisStructureDemoService;

    public EngineeringGovernanceController(
            BadCaseService badCaseService,
            PromptGovernanceService promptGovernanceService,
            RedisStructureDemoService redisStructureDemoService
    ) {
        this.badCaseService = badCaseService;
        this.promptGovernanceService = promptGovernanceService;
        this.redisStructureDemoService = redisStructureDemoService;
    }

    /**
     * 工程治理总览页。
     *
     * <p>这个接口不是业务推理链路的一部分，而是专门把 bad case、Prompt 治理和
     * Redis 示例统一展示给管理端。Agent Memory 已迁移到独立 Agent 服务。</p>
     */
    @GetMapping("/admin/engineering")
    public Map<String, Object> overview() {
        return Map.of(
                "badCases", badCaseService.list(),
                "promptGovernance", promptGovernanceService.snapshot(),
                "interviewCoverage", Map.of(
                        "badCase", "Prompt, model params, output, expected output, root cause, fix strategy, and evaluation evidence are stored together.",
                        "promptContract", "Output schema and post-process chain make model behavior auditable.",
                        "redisStructures", "String/Hash/ZSet/Stream examples map Redis basics back to this gateway project."
                )
        );
    }

    /**
     * 查询所有 bad case。
     */
    @GetMapping("/admin/engineering/bad-cases")
    public Object badCases() {
        return badCaseService.list();
    }

    /**
     * 新建 bad case 复盘记录。
     */
    @PostMapping("/admin/engineering/bad-cases")
    public Object createBadCase(@RequestBody BadCaseService.BadCaseRequest request) {
        return badCaseService.create(request);
    }

    /**
     * 关闭 bad case，并补充修复策略和评测证据。
     */
    @PostMapping("/admin/engineering/bad-cases/{id}/resolve")
    public Object resolveBadCase(@PathVariable String id, @RequestBody BadCaseService.ResolveBadCaseRequest request) {
        return badCaseService.resolve(id, request);
    }

    /**
     * 查询 Prompt 治理资产，包括输出契约、后处理链和实验记录。
     */
    @GetMapping("/admin/engineering/prompt-governance")
    public Object promptGovernance() {
        return promptGovernanceService.snapshot();
    }

    /**
     * 新增或更新模型输出契约。
     */
    @PutMapping("/admin/engineering/output-contracts")
    public Object upsertOutputContract(@RequestBody PromptGovernanceService.OutputContract request) {
        return promptGovernanceService.upsertContract(request);
    }

    /**
     * 使用输出契约校验一段模型输出。
     */
    @PostMapping("/admin/engineering/output-contracts/{contractId}/validate")
    public Object validateOutput(@PathVariable String contractId, @RequestBody JsonNode output) {
        return promptGovernanceService.validate(contractId, output);
    }

    /**
     * 新增或更新后处理链。
     */
    @PutMapping("/admin/engineering/post-process-chains")
    public Object upsertPostProcessChain(@RequestBody PromptGovernanceService.PostProcessChain request) {
        return promptGovernanceService.upsertPostProcessChain(request);
    }

    /**
     * 记录 Prompt 修复实验。
     */
    @PostMapping("/admin/engineering/prompt-experiments")
    public Object recordPromptExperiment(@RequestBody PromptGovernanceService.PromptExperimentRequest request) {
        return promptGovernanceService.recordExperiment(request);
    }

    /**
     * 触发 Redis 数据结构演示。
     */
    @PostMapping("/admin/engineering/redis/demo")
    public Object redisDemo(
            @RequestParam(value = "userId", required = false) String userId,
            @RequestParam(value = "sessionId", required = false) String sessionId
    ) {
        return redisStructureDemoService.demo(userId, sessionId);
    }
}
