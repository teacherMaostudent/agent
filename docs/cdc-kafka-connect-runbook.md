# Transactional Outbox + Debezium production runbook

## Closed delivery boundary

Control Plane, Agent Runtime and Tool Gateway write their state change and an
outbox row in the same PostgreSQL transaction.  They do **not** synchronously
call Kafka, and production configuration sets `*_GOVERNANCE_DELIVERY_MODE=cdc`
so the application relay cannot double-deliver the same event over HTTP.

`debezium-register` is a one-shot Compose service.  It waits for Kafka Connect,
creates or updates only `agent-platform-outbox`, and fails unless the connector
and every task are `RUNNING`.  The Governance consumer waits for that result.
The connector preserves its logical slot when stopped and starts with
`snapshot.mode=no_data`: the cutover is forward-only, avoiding an accidental
replay of the entire retained audit outbox on a new connector.

## Deployment verification

Before rollout, populate `.env.production` from `.env.production.example` using
a secret manager, then run:

```powershell
$env:PYTHONPATH = "$PWD\platform-infra"
py -3.12 scripts/production_readiness.py
docker compose -f compose.production.yaml --env-file .env.production up -d
py -3.12 scripts/production_readiness.py --connect-url http://localhost:8083
```

The second command is a required release check.  It verifies that the exact
connector is running; an HTTP 200 from Kafka Connect alone is not sufficient.

For Kubernetes or a managed Kafka cluster, run `deploy/debezium/register.py`
as a migration Job with `KAFKA_CONNECT_URL`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD` sourced from workload secrets.  Set
`KAFKA_CONNECT_INTERNAL_TOPIC_REPLICATION_FACTOR=3` and provide a three-broker
Kafka cluster; the single-broker Compose reference keeps its default of `1`
only for local topology validation.

## Recovery and controlled replay

1. Stop only `governance-consumer`; leave PostgreSQL and Kafka Connect running.
   Outbox writes continue and Kafka retains the events.
2. Inspect `/connectors/agent-platform-outbox/status`.  A failed task is a
   deployment incident: do not delete the replication slot before the cause is
   understood, because that loses the CDC position.
3. Correct credentials, schema permissions or Kafka connectivity, then rerun
   the registrar.  Its `PUT /config` operation is idempotent and waits for task
   recovery.
4. Restart the consumer with the same consumer group.  Governance deduplicates
   by `event_id`, so at-least-once replay is safe.
5. For a deliberate historical replay, use a new connector name, new slot and
   a separate replay topic/consumer group.  Never reset the production slot or
   route a historical snapshot into the live topic.

## Ownership and alerting

Alert when connector/task state is not `RUNNING`, replication-slot lag grows,
Kafka Connect offset commits stall, the governance DLQ grows, or the consumer
group lag exceeds its SLO.  Database retention must retain WAL long enough for
the agreed outage window; monitor slot lag so an abandoned slot cannot exhaust
disk.  Governance remains the idempotency boundary, but deduplication is not a
substitute for a healthy CDC pipeline.
