# ADR-0002: SQLite ledger, not PostgreSQL

**Date:** 2026-11-16 · **Status:** Accepted · **Supersedes:** the earlier
(unmerged) "native PostgreSQL 18" proposal

## Context

An early proposal suggested a native PostgreSQL service for persistence. The
shipped Forge runtime has always used a single SQLite file: tasks,
transitions, gate results and Forge Trace handoffs are written transactionally
with append-only triggers. That is enough for one local machine and keeps the
project dependency-free.

## Decision

SQLite is the ledger store. `TaskLedger` opens one file (default
`forge.sqlite3`, override with `FORGE_DB`), enables foreign keys, and enforces
append-only audit tables with SQL triggers. Approval invariants, atomic task
claiming and hash-stamped gate results are implemented in SQLite transactions.

PostgreSQL is not used, and no PostgreSQL service, credentials, Docker
container or `.env` file is required. A future multi-machine deployment can
replace `TaskLedger` behind the same API; that would be a new ADR.

## Consequences

- Zero services to start: tests, the demo and the UI use temp files or a single
  local database.
- `sqlite3` is stdlib, so there is no driver dependency.
- Cross-process writes are serialized by SQLite; task claiming is a
  conditional UPDATE in one transaction, so two workers cannot claim the same
  task.
- The ledger is a local file: backup and migration are file-level operations.
