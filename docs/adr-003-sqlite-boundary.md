# ADR 003 SQLite boundary

## Context

Alertfold has to stay runnable with one command, and the invariants under test are about transactions, not about a fleet. PostgreSQL would be the right database for more than one API or worker process. Pretending this repository already is that deployment would hide the limit.

## Decision

Keep SQLite as the only database in this repository. The schema and the worker assume one host: `BEGIN IMMEDIATE`, a partial unique index for one open incident, and a 30-second processing lease that is just a `due_at` timestamp.

These invariants have to survive a move to PostgreSQL:

- Tenant predicates and uniqueness stay in the schema, not only in handlers.
- An incident (or its transition) and its outbox row commit in the same transaction.
- A worker claims a due message before delivery and does not mark it delivered unless it still owns the `processing` status.
- Dead-letter requeue is an explicit admin action. It does not happen because a process restarted.

## Consequences

Tests run without a database service. They do not prove row-level security, multi-host leasing, or load. A second API process against this file would be an operational mistake, not a configuration this project supports.
