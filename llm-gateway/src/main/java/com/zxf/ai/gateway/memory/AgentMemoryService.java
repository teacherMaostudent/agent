package com.zxf.ai.gateway.memory;

import com.zxf.ai.gateway.persistence.RuntimeStateRepository;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.stereotype.Service;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
@ConditionalOnProperty(prefix = "gateway.compatibility.agent-memory", name = "enabled", havingValue = "true")
public class AgentMemoryService {
    /**
     * 四层记忆分别保存，避免把不同生命周期、不同可信度的信息混在一起。
     */
    private static final String PROJECT_RULE = "memory-project-rule";
    private static final String USER_PREFERENCE = "memory-user-preference";
    private static final String SESSION_CONTEXT = "memory-session-context";
    private static final String REFLECTION = "memory-reflection";

    private final RuntimeStateRepository stateRepository;
    private final List<MemoryItem> memory = new ArrayList<>();

    /** 选择可选持久化仓储；未配置时仅用于本地兼容演示的内存实现。 */
    public AgentMemoryService(ObjectProvider<RuntimeStateRepository> stateRepository) {
        this.stateRepository = stateRepository.getIfAvailable();
    }

    /**
     * 写入一条记忆。
     *
     * <p>layer 决定写入 project/user/session/reflection 哪一层。
     * 面试里可用它解释 Claude Code Memory 或 Agent Memory 为什么要分层。</p>
     */
    public synchronized MemoryItem put(MemoryWriteRequest request) {
        String layer = normalizeLayer(request.layer());
        MemoryItem item = new MemoryItem(
                UUID.randomUUID().toString(),
                layer,
                request.scopeId(),
                request.content(),
                request.metadata() == null ? Map.of() : request.metadata(),
                Instant.now()
        );
        if (stateRepository != null) {
            stateRepository.saveDocument(kind(layer), item.id(), item);
        } else {
            memory.add(item);
        }
        return item;
    }

    /**
     * 构造一次会话可使用的上下文视图。
     *
     * <p>注意 reflectionNotes 只返回给调用方观察，不默认注入普通聊天上下文，
     * 这是为了避免“反思结果污染用户上下文”。</p>
     */
    public synchronized Map<String, Object> context(String userId, String sessionId) {
        return Map.of(
                "projectRules", list(PROJECT_RULE),
                "userPreferences", list(USER_PREFERENCE).stream()
                        .filter(item -> item.scopeId() == null || item.scopeId().equals(userId))
                        .toList(),
                "sessionContext", list(SESSION_CONTEXT).stream()
                        .filter(item -> item.scopeId() == null || item.scopeId().equals(sessionId))
                        .toList(),
                "reflectionNotes", list(REFLECTION),
                "isolationRule", "Reflection notes are not injected into normal chat context unless an evaluator explicitly asks for them."
        );
    }

    /**
     * 管理端查看全部分层记忆。
     */
    public synchronized Map<String, Object> snapshot() {
        return Map.of(
                "store", stateRepository == null ? "memory" : "mysql",
                "projectRules", list(PROJECT_RULE),
                "userPreferences", list(USER_PREFERENCE),
                "sessionContext", list(SESSION_CONTEXT),
                "reflectionNotes", list(REFLECTION)
        );
    }

    /**
     * 按 kind 读取某一层记忆。
     */
    private List<MemoryItem> list(String kind) {
        if (stateRepository != null) {
            return stateRepository.listDocuments(kind, MemoryItem.class);
        }
        return memory.stream().filter(item -> kind(item.layer()).equals(kind)).toList();
    }

    /**
     * 兼容多种 layer 写法，避免接口调用方大小写或命名风格不同导致落错层。
     */
    private String normalizeLayer(String layer) {
        if (layer == null || layer.isBlank()) {
            return "session";
        }
        return switch (layer.trim().toLowerCase()) {
            case "project", "project-rule", "project_rules" -> "project";
            case "user", "user-preference", "user_preferences" -> "user";
            case "reflection", "reflection-note", "reflection_notes" -> "reflection";
            default -> "session";
        };
    }

    /**
     * 将业务层 layer 映射为持久化 kind。
     */
    private String kind(String layer) {
        return switch (normalizeLayer(layer)) {
            case "project" -> PROJECT_RULE;
            case "user" -> USER_PREFERENCE;
            case "reflection" -> REFLECTION;
            default -> SESSION_CONTEXT;
        };
    }

    /** 描述一次分层记忆写入，包括隔离范围、内容与附加元数据。 */
    public record MemoryWriteRequest(String layer, String scopeId, String content, Map<String, Object> metadata) {
    }

    /** 表达已写入的不可变记忆项及其作用域和创建时间。 */
    public record MemoryItem(String id, String layer, String scopeId, String content, Map<String, Object> metadata, Instant createdAt) {
    }
}
