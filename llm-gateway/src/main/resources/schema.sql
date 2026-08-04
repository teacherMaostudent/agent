CREATE TABLE IF NOT EXISTS llm_runtime_documents (
    kind VARCHAR(80) NOT NULL,
    doc_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_llm_runtime_kind_updated
ON llm_runtime_documents(kind, updated_at DESC);

CREATE TABLE IF NOT EXISTS llm_request_cache (
    cache_key VARCHAR(128) PRIMARY KEY,
    tenant_id VARCHAR(120) NOT NULL,
    response_payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_request_cache_tenant ON llm_request_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_request_cache_expires_at ON llm_request_cache(expires_at);

CREATE TABLE IF NOT EXISTS llm_cache_stats (
    stat_key VARCHAR(80) PRIMARY KEY,
    stat_value BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
