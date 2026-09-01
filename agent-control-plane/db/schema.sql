PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agent_versions (
    tenant_id TEXT NOT NULL,
    version_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE (tenant_id, agent_id, semantic_version),
    FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_lookup
    ON agent_versions (tenant_id, agent_id, published_at DESC);

-- Tool assets follow the same draft -> immutable version -> reviewed release boundary as agents.
CREATE TABLE IF NOT EXISTS tools (
    tenant_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    owner_team TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, tool_id)
);

CREATE TABLE IF NOT EXISTS tool_versions (
    tenant_id TEXT NOT NULL,
    version_id TEXT PRIMARY KEY,
    tool_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    runtime_definition_json TEXT NOT NULL,
    status TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, tool_id, semantic_version),
    FOREIGN KEY (tenant_id, tool_id) REFERENCES tools (tenant_id, tool_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_versions_lookup
    ON tool_versions (tenant_id, tool_id, published_at DESC);

CREATE TABLE IF NOT EXISTS tool_reviews (
    review_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    comment TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES tool_versions (version_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_reviews_version ON tool_reviews (tenant_id, version_id, reviewed_at DESC);

CREATE TABLE IF NOT EXISTS tool_runtime_releases (
    release_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    FOREIGN KEY (version_id) REFERENCES tool_versions (version_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_active_release
    ON tool_runtime_releases (tenant_id, tool_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS skills (
    tenant_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, skill_id)
);

CREATE TABLE IF NOT EXISTS skill_versions (
    tenant_id TEXT NOT NULL,
    version_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    artifact_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, skill_id, semantic_version),
    FOREIGN KEY (tenant_id, skill_id) REFERENCES skills (tenant_id, skill_id)
);

CREATE INDEX IF NOT EXISTS idx_skill_versions_lookup
    ON skill_versions (tenant_id, skill_id, published_at DESC);

CREATE TABLE IF NOT EXISTS workflows (
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workflow_id)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    tenant_id TEXT NOT NULL,
    version_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    artifact_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE (tenant_id, workflow_id, semantic_version),
    FOREIGN KEY (tenant_id, workflow_id) REFERENCES workflows (tenant_id, workflow_id)
);

CREATE TABLE IF NOT EXISTS workflow_releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES workflow_versions (version_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_releases_resolve
    ON workflow_releases (tenant_id, workflow_id, environment, status, created_at DESC);

CREATE TABLE IF NOT EXISTS releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    rollout_percentage INTEGER NOT NULL,
    tenant_allowlist_json TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_release_id TEXT,
    reason TEXT NOT NULL,
    quality_gate_id TEXT,
    quality_gate_metrics_json TEXT NOT NULL DEFAULT '{}',
    agent_lab_experiment_id TEXT,
    runtime_executor_catalog_version TEXT,
    runtime_executor_cluster_id TEXT,
    runtime_executor_catalog_hash TEXT,
    runtime_capability_manifest_digest TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES agent_versions (version_id),
    FOREIGN KEY (previous_release_id) REFERENCES releases (release_id)
);

CREATE INDEX IF NOT EXISTS idx_releases_resolve
    ON releases (tenant_id, agent_id, environment, created_at DESC);

CREATE TABLE IF NOT EXISTS session_bindings (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    session_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    assignment TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, environment, session_id),
    FOREIGN KEY (release_id) REFERENCES releases (release_id)
);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Tenant is a platform aggregate, not a free-form attribute on a login account.
-- Retired rows remain for audit and historical tenant_id foreign references.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'retired')),
    data_region TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status, tenant_id);

CREATE TABLE IF NOT EXISTS outbox_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox_events (published_at, sequence);

CREATE TABLE IF NOT EXISTS model_route_releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, release_id)
);

CREATE INDEX IF NOT EXISTS idx_model_route_releases_updated
    ON model_route_releases (tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS controller_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
