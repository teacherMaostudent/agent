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

@Repository
@ConditionalOnProperty(prefix = "gateway.persistence", name = "enabled", havingValue = "true")
public class RuntimeStateRepository {
    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;

    public RuntimeStateRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
    }

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

    public void deleteKind(String kind) {
        jdbcTemplate.update("DELETE FROM llm_runtime_documents WHERE kind = ?", kind);
    }

    public void deleteDocument(String kind, String docId) {
        jdbcTemplate.update("DELETE FROM llm_runtime_documents WHERE kind = ? AND doc_id = ?", kind, docId);
    }

    public Optional<JsonNode> getCache(String cacheKey) {
        List<JsonNode> result = jdbcTemplate.query("""
                        SELECT response_payload
                        FROM llm_request_cache
                        WHERE cache_key = ? AND expires_at > CURRENT_TIMESTAMP
                        """,
                (rs, rowNum) -> fromJson(rs.getString("response_payload"), JsonNode.class),
                cacheKey);
        if (result.isEmpty()) {
            jdbcTemplate.update("DELETE FROM llm_request_cache WHERE cache_key = ? OR expires_at <= CURRENT_TIMESTAMP", cacheKey);
            return Optional.empty();
        }
        return Optional.of(result.getFirst().deepCopy());
    }

    public void putCache(String cacheKey, String tenantId, JsonNode response, Instant expiresAt) {
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

    public void clearCache() {
        jdbcTemplate.update("DELETE FROM llm_request_cache");
    }

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

    private long stat(String statKey) {
        List<Long> result = jdbcTemplate.query("SELECT stat_value FROM llm_cache_stats WHERE stat_key = ?",
                (rs, rowNum) -> rs.getLong("stat_value"),
                statKey);
        return result.isEmpty() ? 0L : result.getFirst();
    }

    private String toJson(Object payload) {
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (Exception ex) {
            throw new IllegalStateException("Failed to serialize runtime state", ex);
        }
    }

    private <T> T fromJson(String json, Class<T> targetType) {
        try {
            return objectMapper.readValue(json, targetType);
        } catch (Exception ex) {
            throw new IllegalStateException("Failed to deserialize runtime state", ex);
        }
    }
}
