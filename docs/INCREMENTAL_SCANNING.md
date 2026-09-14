# Incremental iCloud scanning

Enable `I2G_INCREMENTAL_SCAN=true`. The first run performs the normal full
traversal. Subsequent runs consume pyicloud's zone change feed and revisit only
changed or unfinished assets. `I2G_FULL_SCAN_INTERVAL_HOURS=168` schedules a full
reconciliation every seven days; use a shorter interval if desired. Both options
are local pipeline settings, not separate scheduler jobs.

## Checkpoints and unfinished work

State is stored in `<state_dir>/scans/<account-and-library-hash>.db`. It contains
source IDs, a change cursor, and the last full-scan time, not resource download
URLs or raw cloud records. Keep this database and the migration ledger together.

- A full scan captures its starting cursor **before** enumeration and commits it
  only when enumeration completes. A batch cap or interrupted page cannot mark
  an incomplete full scan complete.
- Delta discovery durably queues events before committing the final cursor.
  A page failure leaves the old cursor, so replay is safe and deduplicated.
- Cursor advancement does not mean upload or deletion succeeded. Failed work and
  confirmed photos awaiting the deletion grace period remain queued for retries.
- Pending assets are freshly hydrated with at most two typed CloudKit record
  lookups. pyicloud's convenience lookup can fall back to a full-library scan,
  so it is intentionally not used. Missing or incomplete records remain queued.
- Deleted/hidden source records are removed from the queue, without inventing
  upload or purge confirmation in the migration ledger. Periodic full scans
  reconcile IDs no longer visible in the source.

Known master-only changes map back through the ledger. Unknown master references,
album changes, or other unsupported record types trigger a full reconciliation
because they may affect assets not directly named by the event. Recognizable
expired-cursor errors also force a fresh full scan; unrelated authentication,
transport, and parsing failures remain errors rather than silently widening the
scan. Inspect the run report's `scan` field for mode, event count, pending count,
and deferred lookups; cursor values are not included in reports.

## Interaction with other features

Explicit library selection uses that library's zone and a separately scoped
queue. Runs with album include/exclude filters retain a full selected traversal
so delta hydration cannot bypass the filter. Dry-run also uses a full planning
traversal and never writes scan state. Other optional preservation failures keep
work pending; no-delete runs acknowledge successfully preserved backups without
requiring source removal. Delta retry order is by ID; full scans retain the
existing source traversal order.

This integration requires pyicloud 2.7 or later; the lock remains on the tested
version. Tests exercise page interruptions, expired cursors, delayed hydration,
master changes, grace periods and retries using synthetic CloudKit fixtures.
No live-library migration was run during development.
