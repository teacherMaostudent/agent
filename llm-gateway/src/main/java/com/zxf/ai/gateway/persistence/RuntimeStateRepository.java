package com.zxf.ai.gateway.persistence;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * PostgreSQL-backed state for cache and operational documents.
 *
 * <p>JSON is stored as JSONB so callers can evolve document shapes without a
 * schema migration, while cache reads still enforce expiry in SQL rather than
 * trusting application memory across gateway replicas.</p>
 */
@Repository
@ConditionalOnProperty(prefix = "gateway.persistence", name = "enabled", havingValue = "true")
public class RuntimeStateRepository {
    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    /** 注入受连接池管理的 JDBC 模板与统一 JSON 序列化器。 */
    public RuntimeStateRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

    /** 按业务种类和文档标识原子写入 JSONB 状态，存在时更新而不新增重复记录。 */
    public void saveDocument(String kind, String docId, Object payload) {
        String json = toJson(payload);
        jdbcTemplate.update("""
                        INSERT INTO llm_runtime_documents(kind, doc_id, payload)
                        VALUES (?, ?, CAST(? AS JSONB))
                        ON CONFLICT(kind, doc_id) DO UPDATE
                        SET payload = excluded.payload, updated_at = CURRENT_TIMESTAMP
                        """,
                kind, docId, json);
    }

    /** 读取指定种类和标识的状态文档；不存在时返回空值而非伪造默认对象。 */
    public <T> Optional<T> findDocument(String kind, String docId, Class<T> targetType) {
        List<T> result = jdbcTemplate.query("""
                        SELECT payload
                        FROM llm_runtime_documents
                        WHERE kind = ? AND doc_id = ?
                        """,
                (rs, rowNum) -> fromJson(rs.getString("payload"), targetType),
                kind,
                docId);
        return result.stream().findFirst();
    }

    /** 按最近更新时间倒序列出同一种类状态文档，供管理快照和运行恢复使用。 */
    public <T> List<T> listDocuments(String kind, Class<T> targetType) {
        return jdbcTemplate.query("""
                        SELECT payload
                        FROM llm_runtime_documents
                        WHERE kind = ?
                        ORDER BY updated_at DESC
                        """,
                (rs, rowNum) -> fromJson(rs.getString("payload"), targetType),
                kind);
    }

    /** 删除一种业务状态的全部文档；调用方须先确认其治理授权与影响范围。 */
    public void deleteKind(String kind) {
        jdbcTemplate.update("DELETE FROM llm_runtime_documents WHERE kind = ?", kind);
    }

    /** 删除指定业务状态文档，不影响同种类的其他记录。 */
    public void deleteDocument(String kind, String docId) {
        jdbcTemplate.update("DELETE FROM llm_runtime_documents WHERE kind = ? AND doc_id = ?", kind, docId);
    }

    /** 读取未过期缓存并深拷贝结果；缓存未命中时顺带清理对应过期数据。 */
    public Optional<JsonNode> getCache(String cacheKey) {
        List<JsonNode> result = jdbcTemplate.query("""
                        SELECT response_payload
                        FROM llm_request_cache
                        WHERE cache_key = ? AND expires_at > CURRENT_TIMESTAMP
                        """,
                (rs, rowNum) -> fromJson(rs.getString("response_payload"), JsonNode.class),
                cacheKey);
        if (result.isEmpty()) {
            // Delete expired data on a miss so an absent background cleanup job
            // cannot make the cache table grow without bound.
            jdbcTemplate.update("DELETE FROM llm_request_cache WHERE cache_key = ? OR expires_at <= CURRENT_TIMESTAMP", cacheKey);
            return Optional.empty();
        }
        return Optional.of(result.getFirst().deepCopy());
    }

    /** 以租户归属和绝对过期时间写入缓存，防止跨副本仅依赖进程内 TTL。 */
    public void putCache(String cacheKey, String tenantId, JsonNode response, Instant expiresAt) {
        // The cache key is already tenant-scoped by RequestCacheService; keep
        // tenant id with the row to preserve an auditable ownership boundary.
        jdbcTemplate.update("""
                        INSERT INTO llm_request_cache(cache_key, tenant_id, response_payload, expires_at)
                        VALUES (?, ?, CAST(? AS JSONB), ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            tenant_id = excluded.tenant_id,
                            response_payload = excluded.response_payload,
                            expires_at = excluded.expires_at,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                cacheKey,
                tenantId,
                toJson(response),
                Timestamp.from(expiresAt));
    }

    /** 汇总当前有效缓存和命中统计，供运维端观察缓存收益与容量。 */
    public Map<String, Object> cacheSnapshot() {
        Long entries = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM llm_request_cache WHERE expires_at > CURRENT_TIMESTAMP", Long.class);
        Long hits = stat("cache_hits");
        Long misses = stat("cache_misses");
        return Map.of(
                "entries", entries == null ? 0L : entries,
                "hits", hits,
                "misses", misses
        );
    }

    /** 清空全部缓存记录；仅应由明确授权的运维恢复流程调用。 */
    public void clearCache() {
        jdbcTemplate.update("DELETE FROM llm_request_cache");
    }

    /** 原子增加指定缓存统计计数器，避免多副本竞争导致丢失计数。 */
    public void incrementStat(String statKey) {
        jdbcTemplate.update("""
                        INSERT INTO llm_cache_stats(stat_key, stat_value)
                        VALUES (?, 1)
                        ON CONFLICT(stat_key) DO UPDATE
                        SET stat_value = llm_cache_stats.stat_value + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                statKey);
    }

    /** 查询统计计数器；尚未创建的指标按照零处理。 */
    private long stat(String statKey) {
        List<Long> result = jdbcTemplate.query("SELECT stat_value FROM llm_cache_stats WHERE stat_key = ?",
                (rs, rowNum) -> rs.getLong("stat_value"),
                statKey);
        return result.isEmpty() ? 0L : result.getFirst();
    }

    /** 将领域对象序列化为 JSONB 可写入字符串，失败时中止写入而不存储不完整数据。 */
    private String toJson(Object payload) {
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (Exception ex) {
            throw new IllegalStateException("Failed to serialize runtime state", ex);
        }
    }

    /** 将已持久化 JSON 恢复为目标领域类型，格式异常时显式报告状态损坏。 */
    private <T> T fromJson(String json, Class<T> targetType) {
        try {
            return objectMapper.readValue(json, targetType);
        } catch (Exception ex) {
            throw new IllegalStateException("Failed to deserialize runtime state", ex);
        }
    }
}
