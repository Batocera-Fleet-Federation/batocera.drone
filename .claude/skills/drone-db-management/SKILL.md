---
name: drone-db-management
description: Use this when designing, reviewing, debugging, or modifying Drone SQLite database schemas, migrations, queries, indexes, pagination, relational modeling, local Batocera persistence, ROM metadata storage, save/config tracking, or Drone performance.
---

# Drone Database Management Skill

## Goal

Ensure the Drone application uses SQLite in a lean, relational, performant, and maintainable way.

The Drone database should be treated as the local system of record for durable Batocera-side state. Application code should avoid holding large data sets in memory, avoid unnecessary blob storage, and rely on efficient relational queries, indexes, constraints, and pagination.

The Drone runs on Batocera hardware, which may have limited CPU, memory, and storage. Database design should keep the local application responsive and avoid expensive work during UI/API requests.

## Project Context

Drone uses SQLite for local persistence.

The database is expected to run locally on the Batocera machine.

The Drone database may store or track data related to:

- Drone identity
- registration state
- peer pairing/connection state
- system information
- ROM metadata
- ROM hash cache
- save files
- config files
- sync state
- scan history
- local jobs
- local events or audit records

SQLite should be used as a real relational database, not just as a key/value blob store.

## Core Database Principles

When working on Drone database changes, follow these rules:

1. Favor relational schema design over blob storage.
2. Use foreign keys for relationships between entities when SQLite foreign keys are enabled.
3. Use indexes for common joins, filters, lookups, sorting, and pagination.
4. Use pagination for list endpoints and queries.
5. Avoid loading full tables or large result sets into application memory.
6. Avoid JSON/blob columns unless the data is truly unstructured, rarely queried, or externally sourced.
7. Prefer normalized tables when the application needs to filter, join, search, count, sort, or update individual fields.
8. Keep database work inside SQLite when appropriate instead of pulling large data into Python code.
9. Use migrations or explicit schema versioning for schema changes.
10. Make database changes backward-compatible when possible.
11. Keep writes batched and transactional.
12. Avoid long-running synchronous database work in API/UI request paths.

## SQLite-Specific Rules

SQLite is appropriate for the Drone because it is local, embedded, simple, and reliable for small-to-medium local application storage.

When using SQLite:

1. Enable foreign key enforcement on every connection:

```sql
PRAGMA foreign_keys = ON;
```

2. Consider WAL mode for better read/write behavior:

```sql
PRAGMA journal_mode = WAL;
```

3. Use transactions for bulk inserts and updates:

```sql
BEGIN;
-- many inserts/updates
COMMIT;
```

4. Avoid committing once per row during large scans.

5. Avoid repeatedly opening and closing connections inside tight loops.

6. Use parameterized queries.

7. Avoid table scans on large ROM tables.

8. Use `EXPLAIN QUERY PLAN` to inspect slow queries.

9. Keep the schema simple and explicit.

10. Avoid storing large binary blobs in SQLite unless there is a strong reason.

## Relational Modeling Rules

When reviewing or creating schema, check for:

- Primary keys on every table.
- Foreign keys for parent-child relationships.
- Unique constraints for natural uniqueness.
- Indexes on foreign key columns.
- Indexes on frequently filtered columns.
- Composite indexes for common multi-column filters.
- Explicit `created_at` and `updated_at` timestamps where useful.
- Proper cascading behavior for deletes and updates.
- Avoidance of duplicate state across tables.
- Reasonable storage size for Batocera hardware.

Prefer this:

```sql
CREATE TABLE IF NOT EXISTS systems (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  display_name TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  file_size INTEGER,
  modified_time REAL,
  md5_hash TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_roms_system_id ON roms(system_id);
CREATE INDEX IF NOT EXISTS idx_roms_name ON roms(name);
CREATE INDEX IF NOT EXISTS idx_roms_last_seen_at ON roms(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_roms_system_name_id ON roms(system_id, name, id);
```

Avoid this when fields need to be queried independently:

```sql
CREATE TABLE IF NOT EXISTS rom_inventory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT NOT NULL
);
```

JSON/blob storage is acceptable only when:

- the structure varies significantly,
- the fields are not commonly queried,
- the data is external metadata,
- the data is small,
- or the JSON field is only cached display data and not core relational state.

## Pagination Rules

All list-style APIs and database reads should use pagination.

Avoid:

```sql
SELECT * FROM roms;
```

Prefer:

```sql
SELECT id, system_id, name, path, file_size, md5_hash
FROM roms
WHERE system_id = ?
ORDER BY name ASC, id ASC
LIMIT ? OFFSET ?;
```

For large tables, prefer keyset pagination over deep offset pagination:

```sql
SELECT id, system_id, name, path, file_size, md5_hash
FROM roms
WHERE system_id = ?
  AND (name > ? OR (name = ? AND id > ?))
ORDER BY name ASC, id ASC
LIMIT ?;
```

When adding pagination, return metadata such as:

```json
{
  "items": [],
  "limit": 100,
  "nextCursor": "...",
  "hasMore": true
}
```

Do not design endpoints that return all ROMs, all saves, all configs, all scan results, or all sync records without pagination.

## ROM Metadata and Hash Cache Rules

ROM scanning and hash calculation must be designed for performance.

Rules:

1. Do not hash every ROM on every refresh.
2. Store file path, file size, modified time, hash, and last seen time.
3. Reuse existing hashes when path, file size, and modified time are unchanged.
4. Rehash only when the file appears changed.
5. Detect deleted ROMs by `last_seen_at` or scan batch tracking.
6. Batch database writes during scans.
7. Avoid blocking UI/API requests while scanning.
8. Track scan progress separately from ROM records.
9. Avoid duplicate ROM discovery across overlapping paths.
10. Avoid hashing the same file twice in the same scan.

Recommended ROM hash cache schema:

```sql
CREATE TABLE IF NOT EXISTS rom_hash_cache (
  rom_path TEXT PRIMARY KEY,
  file_size INTEGER NOT NULL,
  modified_time REAL,
  md5_hash TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rom_hash_cache_size ON rom_hash_cache(file_size);
CREATE INDEX IF NOT EXISTS idx_rom_hash_cache_last_seen ON rom_hash_cache(last_seen_at);
```

Recommended scan batch schema:

```sql
CREATE TABLE IF NOT EXISTS scan_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  total_files INTEGER DEFAULT 0,
  scanned_files INTEGER DEFAULT 0,
  hashed_files INTEGER DEFAULT 0,
  reused_hashes INTEGER DEFAULT 0,
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_scan_batches_status ON scan_batches(status);
CREATE INDEX IF NOT EXISTS idx_scan_batches_started_at ON scan_batches(started_at);
```

## Performance Rules

When reviewing query performance:

1. Check whether the query uses indexes.
2. Check whether joins are supported by indexes.
3. Avoid N+1 query patterns.
4. Avoid loading large tables into application code for filtering.
5. Avoid repeated JSON parsing in application code.
6. Use aggregate queries instead of counting in code.
7. Use `EXPLAIN QUERY PLAN` for slow queries.
8. Add indexes based on real access patterns.
9. Do not add excessive indexes that slow writes without clear read benefit.
10. Batch inserts and updates where appropriate.
11. Keep UI/API endpoints responsive.
12. Avoid filesystem scans during page loads.

Useful commands:

```bash
sqlite3 /userdata/system/bff/drone.db ".tables"
sqlite3 /userdata/system/bff/drone.db ".schema"
sqlite3 /userdata/system/bff/drone.db "PRAGMA foreign_keys;"
sqlite3 /userdata/system/bff/drone.db "PRAGMA journal_mode;"
sqlite3 /userdata/system/bff/drone.db "EXPLAIN QUERY PLAN SELECT ...;"
```

If the project uses a different database path, inspect the code or environment before assuming the path.

## Indexing Rules

Create indexes for:

- foreign key columns,
- lookup columns,
- frequently filtered columns,
- frequently sorted columns,
- pagination cursors,
- unique business identifiers,
- status fields used in dashboards or background jobs,
- file paths,
- scan batch IDs,
- sync job IDs.

Examples:

```sql
CREATE INDEX IF NOT EXISTS idx_roms_system_id ON roms(system_id);
CREATE INDEX IF NOT EXISTS idx_roms_name ON roms(name);
CREATE INDEX IF NOT EXISTS idx_roms_system_name_id ON roms(system_id, name, id);
CREATE INDEX IF NOT EXISTS idx_roms_file_size ON roms(file_size);
CREATE INDEX IF NOT EXISTS idx_roms_modified_time ON roms(modified_time);
CREATE INDEX IF NOT EXISTS idx_save_files_path ON save_files(path);
CREATE INDEX IF NOT EXISTS idx_config_files_path ON config_files(path);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_created_at ON sync_jobs(created_at);
```

Use unique constraints when applicable:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_roms_path_unique
ON roms(path);
```

For keyset pagination:

```sql
CREATE INDEX IF NOT EXISTS idx_roms_system_name_id
ON roms(system_id, name, id);
```

## Blob and JSON Storage Rules

Avoid blob or JSON storage for data that needs to be:

- filtered,
- joined,
- counted,
- sorted,
- searched,
- updated field-by-field,
- used in sync comparisons,
- displayed in paginated UI pages,
- used for determining whether a file changed.

Instead of storing this:

```json
{
  "roms": [
    {
      "name": "Example",
      "system": "nes",
      "path": "/userdata/roms/nes/example.zip",
      "size": 12345,
      "hash": "..."
    }
  ]
}
```

Prefer relational tables:

```sql
CREATE TABLE IF NOT EXISTS systems (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS roms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  file_size INTEGER,
  modified_time REAL,
  md5_hash TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_roms_system_id ON roms(system_id);
CREATE INDEX IF NOT EXISTS idx_roms_system_name_id ON roms(system_id, name, id);
```

## Migration and Schema Version Rules

Drone schema changes must be tracked through migrations or explicit schema versioning.

Any database change should be represented in the project’s migration/schema setup, including:

- creating tables,
- altering tables,
- dropping tables,
- adding columns,
- removing columns,
- renaming columns,
- changing column types,
- adding constraints,
- removing constraints,
- adding foreign keys,
- removing foreign keys,
- adding indexes,
- removing indexes,
- adding seed/reference data,
- moving data from blobs into relational tables,
- backfilling new relational tables or columns.

Do not make manual schema changes directly against the local SQLite database unless explicitly directed for emergency troubleshooting. If a manual change is made, create a matching migration or schema update immediately so the repository remains the source of truth.

Before creating a migration or schema update:

1. Inspect current schema.
2. Check existing migrations or schema initialization logic.
3. Understand current data shape.
4. Confirm whether data backfill is required.
5. Confirm whether existing Batocera installs need upgrade handling.
6. Ensure rollback or safe failure behavior where possible.

Useful schema inspection:

```bash
sqlite3 /userdata/system/bff/drone.db ".schema"
sqlite3 /userdata/system/bff/drone.db "PRAGMA table_info(roms);"
sqlite3 /userdata/system/bff/drone.db "PRAGMA index_list(roms);"
sqlite3 /userdata/system/bff/drone.db "PRAGMA foreign_key_list(roms);"
```

Avoid destructive changes unless explicitly requested.

Risky changes include:

- dropping columns,
- dropping tables,
- rewriting large tables,
- changing primary keys,
- changing foreign key behavior,
- converting JSON blobs into relational tables without a backfill plan,
- deleting local sync state,
- deleting hash cache without a reason.

## Batocera Local Persistence Rules

The Drone database lives on the Batocera machine and should be stored in a persistent location under `/userdata`.

Common expected pattern:

```text
/userdata/system/bff/drone.db
```

If the project uses a different path, follow the project code and configuration.

Do not store durable state in temporary locations that may be cleared on reboot.

Avoid putting the database inside application package directories that may be replaced during updates.

The Drone should survive:

- application restart,
- Batocera reboot,
- network outage,
- peer/network outage,
- local service restart.

## Save and Config Tracking Rules

For save/config tracking, favor relational file records.

Example schema:

```sql
CREATE TABLE IF NOT EXISTS tracked_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_type TEXT NOT NULL,
  system_id INTEGER REFERENCES systems(id) ON DELETE SET NULL,
  path TEXT NOT NULL UNIQUE,
  file_size INTEGER,
  modified_time REAL,
  md5_hash TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracked_files_file_type ON tracked_files(file_type);
CREATE INDEX IF NOT EXISTS idx_tracked_files_system_id ON tracked_files(system_id);
CREATE INDEX IF NOT EXISTS idx_tracked_files_last_seen_at ON tracked_files(last_seen_at);
```

Use `file_type` values such as:

```text
save
config
state
bios
metadata
```

Do not store entire save files or config files as blobs unless explicitly required. Prefer path, metadata, hash, sync status, and file timestamps.

## Sync State Rules

Sync state should be explicit and queryable.

Example schema:

```sql
CREATE TABLE IF NOT EXISTS sync_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  peer_job_id TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS sync_job_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  item_type TEXT NOT NULL,
  local_path TEXT,
  remote_path TEXT,
  status TEXT NOT NULL,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_started_at ON sync_jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_sync_job_items_job_id ON sync_job_items(sync_job_id);
CREATE INDEX IF NOT EXISTS idx_sync_job_items_status ON sync_job_items(status);
```

Avoid keeping sync job state only in memory.

## Query Review Checklist

When reviewing a query, verify:

- Does it use pagination?
- Does it avoid `SELECT *` when only specific columns are needed?
- Does it filter in SQL instead of in application code?
- Does it join on indexed foreign keys?
- Does it avoid N+1 patterns?
- Does it have a deterministic `ORDER BY` for pagination?
- Does it use keyset pagination for large/deep result sets?
- Does it avoid loading large datasets into memory?
- Does it use aggregates in SQL instead of code loops?
- Does it avoid unnecessary JSON extraction?
- Does it avoid expensive filesystem work during query execution?

Bad:

```python
roms = db.execute("SELECT * FROM roms").fetchall()
filtered = [r for r in roms if r["system_id"] == system_id]
```

Good:

```sql
SELECT id, name, system_id, path, file_size, md5_hash
FROM roms
WHERE system_id = ?
ORDER BY name ASC, id ASC
LIMIT ?;
```

## API and Application Rules

Application code should:

1. Request only the data needed.
2. Use pagination on list endpoints.
3. Avoid holding full ROM inventories in memory.
4. Avoid using dictionaries as durable state.
5. Avoid duplicating database state in process memory.
6. Push filtering, sorting, joining, and counting into SQLite.
7. Use database transactions for multi-step writes.
8. Avoid long-running synchronous requests for heavy database operations.
9. Return summaries/counts where full detail is unnecessary.
10. Avoid running filesystem scans during UI/API page loads.
11. Use background jobs or cached scan results for expensive operations.
12. Keep local Batocera resource usage low.

For large collections like ROMs, saves, configs, scan history, sync history, or local events, APIs should expose:

- paginated list endpoint,
- detail endpoint by ID,
- summary/count endpoint,
- filter parameters,
- deterministic sorting,
- cursor or page metadata.

## Common Drone Data Areas

Consider relational modeling for:

- drone identity,
- registration state,
- peer pairing state,
- connection state,
- system info snapshots,
- ROM systems,
- ROM metadata,
- ROM hash cache,
- scan batches,
- scan batch items,
- save files,
- config files,
- sync jobs,
- sync job items,
- local audit events,
- local background jobs,
- API tokens or device tokens.

Example relationship direction:

```text
drone_state
systems
  -> roms
  -> tracked_files
scan_batches
  -> scan_batch_items
sync_jobs
  -> sync_job_items
```

## Expected Output Format

When completing Drone database work, respond using this format:

```text
Root cause / objective:
...

Schema changes:
...

Migration / schema version changes:
...

Indexes added or changed:
...

Query changes:
...

Pagination changes:
...

Blob-to-relational changes:
...

ROM/hash-cache changes:
...

Local SQLite validation:
...

Batocera/runtime considerations:
...

Risks:
...

Files changed:
...
```

## Safety Rules

Do not:

- remove local Drone state without explicit approval,
- delete the SQLite database without explicit approval,
- drop tables without explicit approval,
- run destructive migrations without explicit approval,
- make schema changes outside the project’s migration/schema version process,
- load full local ROM tables into application memory,
- replace relational schema with unstructured blobs,
- remove foreign keys to “make it easier,”
- remove indexes without validating query impact,
- perform full ROM rehashes unnecessarily,
- block UI/API requests on full filesystem scans,
- use in-memory dictionaries as durable state.

## Default Bias

When unsure, choose the option that keeps:

- data relational,
- queries indexed,
- reads paginated,
- application memory low,
- Batocera CPU usage low,
- SQLite schema explicit,
- migrations/schema versions current,
- expensive scans out of request paths,
- local state durable across restarts.