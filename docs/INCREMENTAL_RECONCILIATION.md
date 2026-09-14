# Reconciliation after configuration changes

Incremental scan checkpoints now include a fingerprint of the preservation
policy and a persistent identity for the migration ledger. Changing edited or
Live Photo policy, metadata backfill/archive settings, album preservation,
delete/grace policy, or the configured destination path triggers a full scan
before normal deltas resume. A replacement ledger also requires full discovery.
Credentials and raw configuration values are not written into scan checkpoints.

The policy fingerprint commits atomically with a completed full scan. An
interruption, page error or batch cap cannot acknowledge the new policy; the
next run retries full reconciliation. Existing checkpoints without a fingerprint
receive one full reconciliation on upgrade. Successfully completed unchanged
policies keep the existing incremental behavior. Worker counts and batch tuning
do not by themselves force full scans.

This does not authorize a destination-account change or guess the provenance of
old upload confirmations. It ensures changed requirements are discovered; the
normal preservation/deletion gates still decide whether each asset is complete.
