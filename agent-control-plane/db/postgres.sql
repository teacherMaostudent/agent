CREATE TABLE IF NOT EXISTS agents (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
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
    published_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, agent_id, semantic_version),
    FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_versions_lookup
    ON agent_versions (tenant_id, agent_id, published_at DESC);

CREATE TABLE IF NOT EXISTS skills (
    tenant_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
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
    published_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
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
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
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
    published_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, workflow_id, semantic_version),
    FOREIGN KEY (tenant_id, workflow_id) REFERENCES workflows (tenant_id, workflow_id)
);

CREATE TABLE IF NOT EXISTS workflow_releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES workflow_versions(version_id),
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_releases_resolve
    ON workflow_releases (tenant_id, workflow_id, environment, status, created_at DESC);

CREATE TABLE IF NOT EXISTS releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES agent_versions(version_id),
    environment TEXT NOT NULL,
    rollout_percentage INTEGER NOT NULL,
    tenant_allowlist_json TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_release_id TEXT REFERENCES releases(release_id),
    reason TEXT NOT NULL,
    quality_gate_id TEXT,
    quality_gate_metrics_json TEXT NOT NULL DEFAULT '{}',
    agent_lab_experiment_id TEXT,
    runtime_executor_catalog_version TEXT,
    runtime_executor_cluster_id TEXT,
    runtime_executor_catalog_hash TEXT,
    runtime_capability_manifest_digest TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_releases_resolve
    ON releases (tenant_id, agent_id, environment, created_at DESC);

CREATE TABLE IF NOT EXISTS session_bindings (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    session_id TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    assignment TEXT NOT NULL,
    bound_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, environment, session_id)
);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox_events (published_at, sequence);

CREATE TABLE IF NOT EXISTS model_route_releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, release_id)
);

CREATE TABLE IF NOT EXISTS controller_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE releases ENABLE ROW LEVEL SECURITY;
