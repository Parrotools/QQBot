# Production Hardening Design

## Scope

Strengthen the existing single-process QQ bot for long-running operation without adding business capabilities or external dependencies.

## Database migrations

`Database.connect()` will create a `schema_migrations` ledger and apply ordered, idempotent SQL migrations. Migration 1 contains the current schema as `CREATE ... IF NOT EXISTS`; therefore an existing database without a ledger is adopted without rewriting or deleting its data. Later migrations append only additive tables or indexes. A migration is recorded only after its SQL completes.

## Outbound delivery

Scheduled reminders, GitHub notifications, and daily reports enqueue delivery rows in SQLite. `MessageDispatcher` remains the only QQ API caller and owns a small in-process worker. It claims due rows with a lease, sends them, and stores `sent`, `retry`, or terminal `failed` state. Retries use configured fixed delay and a bounded attempt count; interrupted `sending` rows become claimable after the lease expires. Interactive administrator broadcasts remain immediate so their confirmation result is not changed.

## Status and health

A `HealthService` performs bounded concurrent probes for SQLite, the configured LLM endpoint, and GitHub's rate-limit endpoint. It also returns uptime, scheduler state, GitHub and memory counts, queued delivery state, and recent persisted delivery errors. `/status` renders a safe Chinese summary; `/healthz` returns the same structured checks for deployment monitoring without exposing secrets.

## Compatibility and verification

All identifiers remain strings at the service boundary. Existing tables and their rows are untouched. Each behavior is test-first: old database adoption, queue success/retry/exhaustion/recovery, status output, and health probe failures. Each module runs its targeted tests before continuing, then the complete suite and static checks run at the end.
