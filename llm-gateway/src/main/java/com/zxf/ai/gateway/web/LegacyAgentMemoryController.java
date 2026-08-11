package com.zxf.ai.gateway.web;

import com.zxf.ai.gateway.memory.AgentMemoryService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Compatibility-only API for the former in-gateway Agent memory demo.
 *
 * <p>Production Agent memory belongs to the Agent service. This controller is
 * absent unless the compatibility switch is explicitly enabled.</p>
 */
@RestController
@RequestMapping
@ConditionalOnProperty(prefix = "gateway.compatibility.agent-memory", name = "enabled", havingValue = "true")
public class LegacyAgentMemoryController {
    private final AgentMemoryService memoryService;

    /**
     * 初始化 legacy agent memory controller 所需的依赖与运行期状态。
    */
    public LegacyAgentMemoryController(AgentMemoryService memoryService) {
        this.memoryService = memoryService;
    }

    @GetMapping("/admin/engineering/memory")
    /**
     * 执行 memory 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object memory() {
        return memoryService.snapshot();
    }

    @PostMapping("/admin/engineering/memory")
    /**
     * 执行 write memory 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object writeMemory(@RequestBody AgentMemoryService.MemoryWriteRequest request) {
        return memoryService.put(request);
    }

    @GetMapping("/v1/memory/context")
    /**
     * 执行 memory context 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public Object memoryContext(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestParam(value = "sessionId", required = false) String sessionId
    ) {
        return memoryService.context(
                userId == null ? "anonymous" : userId,
                sessionId == null ? "default" : sessionId
        );
    }
}
