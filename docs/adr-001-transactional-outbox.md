# ADR 001 Transactional outbox

## Context

An incident must exist even if notification delivery is unavailable. Sending a notification inside the request can fail after the database commits or succeed before a rollback.

## Decision

Write the incident and its notification intent in the same SQLite transaction. A separate worker claims due messages, delivers them, and retries failures with bounded exponential backoff. The message ID is stable so a downstream receiver can deduplicate deliveries.

## Consequences

The API returns before notification delivery. Delivery is at least once, since a worker can crash after delivery but before marking the row delivered. SQLite is suitable for the local demonstration and one host, but a multi-host deployment would use PostgreSQL with row locking and a real delivery adapter.
