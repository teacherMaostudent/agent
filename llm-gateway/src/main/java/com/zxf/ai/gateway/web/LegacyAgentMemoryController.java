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
     * 返回兼容端点的只读内存投影；新 Runtime 不依赖该旧接口保存 Session。
    */
    public Object memory() {
        return memoryService.snapshot();
    }

    @PostMapping("/admin/engineering/memory")
    /**
     * 写入兼容内存端点；仅供迁移窗口使用，不替代 Context Service 的会话所有权。
    */
    public Object writeMemory(@RequestBody AgentMemoryService.MemoryWriteRequest request) {
        return memoryService.put(request);
    }

    @GetMapping("/v1/memory/context")
    /**
     * 读取兼容内存上下文并应用租户/会话隔离，不参与新 Context Service 排序。
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
