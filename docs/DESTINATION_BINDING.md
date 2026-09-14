# Destination-bound backup evidence

A live migration requires gotohp to report exactly one marked active account.
The ledger binds to a hash of that effective account and the explicit config
selector, not to the existence of a credentials file. No account address,
credential output or authentication token is stored in the binding or run report.

Changing account or explicit configuration stops the run before old upload
confirmations can authorize deletion. Restore the original selection to resume,
or use a separate `I2G_STATE_DIR` for a genuinely different destination. Refreshing
credentials for the same account in the same config does not invalidate binding.
Do not change the account during a run: checks bracket uploads (including album
passes) and precede source deletion. A changed account discards returned upload
verdicts rather than crediting them to the original destination.

## Upgrading existing state

Older unbound retained upload/linkage confirmations are revalidated once. Deleted
history and failure budgets are retained. A durable reconciliation flag forces
full discovery even when an old incremental queue is empty; it clears only after
complete unfiltered traversal. Interrupted discovery remains retryable.

Album mappings and receipts are scoped by destination. Old unscoped mapping
files are left untouched and are not trusted for the active destination. This
can require rebuilding album mappings (and can create duplicate destination
albums once); use `album-mappings` and `bind-album` for explicit recovery. These
commands operate only on the currently selected destination's namespace.

## gotohp configuration compatibility

The pinned gotohp source can ignore `--config` when loading credentials. With an
explicit `I2G_GOTOHP_CONFIG`, a temporary empty-config probe must not report the
default account; otherwise migration is rejected before discovery or upload.
Use gotohp's active portable/default configuration with `I2G_GOTOHP_CONFIG` unset,
or a compatible build that honors the override. The probe contains no real
credentials and does not modify the existing configuration.

The effective selected account is gotohp's exported account identifier, not a
new Google OAuth identity lookup. This is provenance protection, not verification
that previously uploaded media still exists remotely. No live library migration
is performed by the tests; fake subprocess outputs exercise the account boundary.
Dry-run remains a local planning preview and does not bind a destination.
