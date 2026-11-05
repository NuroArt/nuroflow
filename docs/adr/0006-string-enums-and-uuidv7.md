# ADR-0006: VARCHAR + CHECK enums and application-generated UUIDv7 keys

- **Status:** Accepted
- **Date:** 2026-11-04

## Enums

**Context.** Native Postgres `ENUM` types can gain values (`ALTER TYPE … ADD VALUE`), but can't
easily lose or rename them. Before PG 12, `ADD VALUE` couldn't even run inside a transaction.

**Decision.** Store statuses and types as `VARCHAR(n)` with a named `CHECK` constraint generated
from the Python `StrEnum` (`sa.Enum(..., native_enum=False, create_constraint=True)`).

**Consequences.** Changing an enum means dropping and recreating one constraint: fast, transactional
and reversible. Storage cost is the same as native enums for these short values.

## Primary keys

**Context.** Random UUIDv4 keys scatter inserts across B-tree pages. On write-heavy tables
(`task_runs`, `task_attempts`) that causes page splits and index bloat. Serial integers leak row
counts and can't be generated before insert, which complicates batch creation of runs and tasks.

**Decision.** UUIDv7 (RFC 9562): a 48-bit millisecond timestamp prefix plus randomness, generated in
the application (`nuroflow.core.ids.uuid7`). It's stored in the native `uuid` type. PG 18's built-in
`uuidv7()` can replace the app generator later without a schema change.
`task_logs` uses `BIGINT GENERATED ALWAYS AS IDENTITY`: it's append-only, internal, and benefits
from the smallest key.

**Consequences.** Index-friendly insert order, IDs known before flush, and no enumeration leakage.
