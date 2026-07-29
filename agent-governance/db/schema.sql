PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    source_service TEXT NOT NULL,
    event_type TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_sequence
    ON audit_events (tenant_id, sequence);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_occurred
    ON audit_events (tenant_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_note TEXT,
    FOREIGN KEY (event_id) REFERENCES audit_events (event_id),
    UNIQUE (event_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_findings_tenant_status
    ON findings (tenant_id, status, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Evaluation, compliance and human-review state belongs to Governance.  A
-- single typed-document table avoids one table and repository implementation
-- per workflow while preserving tenant isolation and deterministic IDs.
CREATE TABLE IF NOT EXISTS governance_documents (
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    document_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, kind, document_id)
);

CREATE INDEX IF NOT EXISTS idx_governance_documents_kind_updated
    ON governance_documents (tenant_id, kind, updated_at DESC);
