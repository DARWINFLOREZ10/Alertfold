# ADR 002 Tenant boundaries

## Context

Incident data from different organizations must remain isolated even when a caller knows an incident ID.

## Decision

Administrative API keys map to a tenant and role. Every list, transition and audit query constrains by tenant ID. Webhooks use per-tenant HMAC secrets, and service IDs resolve the tenant before validation. Database uniqueness constraints include tenant ID where appropriate.

## Consequences

Tests cover cross-tenant reads and state changes. This is application-level isolation, not database row-level security. For a hosted service, the next step would be PostgreSQL RLS and regular tests for missing tenant predicates.
