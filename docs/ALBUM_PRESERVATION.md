# Preserve iCloud albums

Set `I2G_PRESERVE_ALBUMS=true` to preserve user album memberships. Unset/false
keeps the current behavior. gotohp must support `upload --album` and its JSON
album acknowledgements; an incompatible binary stops this feature before any
source deletion.

User album IDs, not names, identify mappings. New destination albums are named
`iCloud / <full iCloud folder path>`. Smart albums and folder containers are not
created as Google albums. An asset in several albums is added to each. Names are
chosen at first creation; later source renames/removals do not rename albums or
remove memberships in Google Photos. Empty albums have no migration work and
are not created.

## Verification and persistence

Mappings and membership receipts live in `<state_dir>/albums/<source-scope>.db`,
scoped to the iCloud account and library. Back up these databases with the main
ledger. Receipts cover the current resource fingerprints and destination media
keys. Retries use the recorded album key rather than creating the name again.
The pipeline requires exact per-path media keys and a complete album
acknowledgement before permitting source deletion.

The existing gotohp CLI adds albums through an upload/hash-lookup invocation.
The adapter hard-links one staged file per destination item into an isolated
album pass; duplicate detection remains enabled and no `--force` is used.
A linked Live Photo contributes one destination item, not both component paths.
Previously uploaded assets may need downloading again to provide hash-lookup
files for unfinished album work; their prior upload proof is preserved. No extra
full-sized staging copy is made. A filesystem without hard-link support reports
failure and retains the source. A missing media key, different duplicate key,
partial album response, or album capacity/permission error also retains it.

## Recover an ambiguous album creation

Creation intent is committed before the remote call. If its acknowledgement is
lost, automatically retrying creation could create duplicate albums, so that
mapping stops until the destination is inspected:

```bash
i2g album-mappings
i2g bind-album <source-album-id> <existing-google-album-key>
```

Obtain the complete `AF1Qip...` album key from the existing destination album.
If creation did not happen, create the intended album manually and bind its key.
The bind command validates the source ID and key format, updates local mapping
state only, and causes memberships to be verified on the next run. It can also
pre-bind a source album to an existing destination album. It does not verify
remote permissions itself. Inspect mappings under the same source and Google
account configuration used for migration.

Dry-run performs no album writes. Development tests use synthetic album and
upload responses; no live albums or photo libraries were modified.
