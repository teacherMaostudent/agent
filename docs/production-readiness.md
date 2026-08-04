# Agent Platform production implementation baseline

`compose.platform.yaml` remains the local developer topology. `compose.production.yaml`
is the executable integration reference for the production adapters; it is intentionally
single-node and must not be mistaken for an HA deployment manifest.

## Implemented production stack

| Concern | Selected component | Code integration |
|---|---|---|
| Durable state | PostgreSQL | Separate schemas for Control Plane, Runtime/Context/RAG, Tool Gateway and Governance; SQLite remains local-only |
| Durable workflows | Temporal | Agent runs, ingestion and release monitoring use workflows, activities and retry policies |
| Event delivery | PostgreSQL Outbox + Debezium + Kafka | Canonical Governance events, read-committed consumer, idempotent producer, retry topic, DLQ and explicit replay CLI |
| Source and audit objects | S3-compatible object storage | Content-addressed document objects, multipart/spooled upload, KMS encryption options and Object Lock audit export |
| Retrieval projection | OpenSearch | Strict versioned index, in-index tenant/user ACL, lexical/vector scoring and atomic alias publication |
| Coordination | Redis | Distributed quotas/rate limits; durable business state is not stored in Redis |
| Identity and policy | OIDC JWT + OAuth2 workload identity + OPA | Caller identity headers are replaced after JWT verification; verified workloads may delegate tenant context; policy is fail-closed |
| Telemetry | OpenTelemetry | FastAPI, HTTPX and Spring instrumentation with an OTel Collector redaction pipeline |

The production environment validators reject SQLite, local queues, missing OIDC/JWKS,
missing workload credentials, disabled OPA, wildcard CORS, anonymous access and unrotated
development credentials where applicable.

## Service ownership

- Control Plane compiles immutable release snapshots, checks Governance quality gates and
  starts Temporal release workflows. It does not execute an Agent.
- Runtime resolves a released snapshot, plans and executes the Agent graph, applies cost and
  deadline budgets, checkpoints state and emits canonical events through its outbox.
- Context Service assembles ranked conversation history and evidence under an explainable
  token budget. RAG failure can degrade to memory-only context.
- RAG Service ingests documents from S3 and queries the OpenSearch projection with ACL
  predicates inside the search request.
- LLM Gateway authenticates callers, routes models, enforces quota/cost policy, redacts
  governed payloads and emits normalized USD usage events.
- Tool Gateway applies OPA policy, permission/risk/approval controls, distributed limits,
  idempotency and audit recording before executing an adapter.
- Governance consumes events asynchronously, evaluates quality/compliance and exports a
  verified hash chain with a KMS signature into WORM storage.

## Deployment sequence

1. Provision PostgreSQL with PITR, Kafka, Temporal, Redis, OpenSearch, OPA, the OTel
   Collector and Object-Lock-enabled S3 buckets.
2. Create separate database roles per service and apply the SQL migrations with a migrator
   role. Application roles should not retain schema-owner privileges after bootstrap.
3. Register OIDC confidential clients. Workload tokens must contain the
   `platform-workload` role and target the configured audience.
4. Load the OPA policies from `deploy/opa`, start the applications/workers, then register
   `deploy/debezium/platform-outbox.json` using `scripts/register_debezium.ps1`.
5. Verify readiness, publish a canary release, execute an Agent run and confirm the same
   event ID in PostgreSQL outbox, Kafka and Governance audit storage.
6. Run a WORM export and independently verify its SHA-256, Merkle root and KMS signature.

## HA and security gates outside application code

- Run PostgreSQL, Kafka, Temporal and OpenSearch as multi-zone managed services or supported
  clusters; the reference Compose file has no quorum or failover guarantees.
- Enforce mTLS with a service mesh or workload-aware ingress. OAuth workload tokens provide
  application identity, but do not encrypt or mutually authenticate the transport alone.
- Put secrets in a secret manager and rotate the compatibility API keys. API keys remain a
  secondary capability/model authorization mechanism during migration, never an identity
  source.
- Enable S3 versioning/Object Lock at bucket creation, cross-region replication when policy
  permits, and external Hash Chain anchoring on the required cadence.
- Pin image digests, generate an SBOM, scan dependencies/containers/secrets, sign artifacts
  and enforce admission policy before promotion.

## SLO and recovery acceptance

| Signal | Target | Page when |
|---|---:|---:|
| Runtime accepted submissions | 99.9% | 5-minute error rate > 1% |
| Temporal schedule-to-start p99 | < 30 s | > 60 s for 10 minutes |
| LLM non-stream completion availability | 99.9% | burn rate > 14.4x |
| RAG query p95 | < 1.5 s | > 3 s for 10 minutes |
| Kafka consumer lag / oldest outbox age | < 60 s | > 5 minutes |
| Governance audit-chain validity | 100% | any broken event |

PostgreSQL restore, Kafka replay, Temporal worker loss, OpenSearch rebuild, model timeout,
Redis failover and Object Storage recovery must be exercised before production approval.
The required result is a recorded RPO/RTO report, not merely a successful health check.
