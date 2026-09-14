# Restored iCloud assets

When an asset previously recorded as deleted is observed in the source again,
its old deletion timestamp no longer describes the current source state. The
pipeline clears that timestamp and its old upload/Live Photo linkage evidence,
then reconfirms the current resources. A failed reconfirmation remains in the
incremental retry queue even if no further CloudKit events arrive.

The source master mapping is refreshed. First observation is restarted for the
reappearing asset, including the fallback grace period when iCloud supplies no
import date. Normal unchanged retained assets keep their progress. Deleted
history not observed again remains intact, and dry-run modifies only its
in-memory ledger snapshot. No manual ledger reset is required.
