CREATE TABLE IF NOT EXISTS llm_runtime_documents (
    kind VARCHAR(80) NOT NULL,
    doc_id VARCHAR(160) NOT NULL,
    payload JSON NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (kind, doc_id),
    INDEX idx_llm_runtime_kind_updated (kind, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_request_cache (
    cache_key VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(120) NOT NULL,
    response_payload JSON NOT NULL,
    expires_at TIMESTAMP(3) NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (cache_key),
    INDEX idx_llm_request_cache_tenant (tenant_id),
    INDEX idx_llm_request_cache_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_cache_stats (
    stat_key VARCHAR(80) NOT NULL,
    stat_value BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (stat_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_gmp_review_tasks (
    task_id VARCHAR(160) NOT NULL,
    rag_review_id VARCHAR(160) NULL,
    document_id VARCHAR(160) NULL,
    business_id VARCHAR(160) NULL,
    document_type VARCHAR(160) NOT NULL,
    tenant_id VARCHAR(120) NOT NULL,
    user_id VARCHAR(120) NOT NULL,
    status VARCHAR(60) NOT NULL,
    risk_level VARCHAR(40) NULL,
    summary VARCHAR(1000) NULL,
    need_human_review BOOLEAN NOT NULL DEFAULT FALSE,
    cost DECIMAL(18, 8) NOT NULL DEFAULT 0,
    latency_ms BIGINT NOT NULL DEFAULT 0,
    payload JSON NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (task_id),
    INDEX idx_llm_gmp_review_status (status),
    INDEX idx_llm_gmp_review_tenant (tenant_id),
    INDEX idx_llm_gmp_review_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
