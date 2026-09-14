# Repair existing Google Photos stills into Live Photos

Enable `I2G_UPDATE_EXISTING_PHOTOS_TO_LIVE=true` to ask gotohp to attach a
matching iCloud MOV to an existing Google Photos still. This is opt-in because
it modifies an existing destination item. It requires `I2G_PAIR_LIVE_PHOTOS=true`,
`I2G_INCLUDE_LIVE_PHOTO_VIDEO=true`, and edited policy `both` or `original`.

The pinned gotohp implements `--update-existing-photos-to-live`. The pipeline
checks that capability before downloads. Rebuild older binaries with
`python scripts/fetch_gotohp.py` if the preflight reports it missing.

## Verification and retries

- In repair mode, incomplete pairs are not uploaded as standalone components.
- Both local paths must receive the same successful linked-pair verdict and
  nonempty destination media key before linkage is recorded.
- Enabling repair invalidates legacy still/MOV confirmations once per content
  fingerprint, including exhausted component-exists skips. Already-purged
  history stays unchanged. Repaired fingerprints are reused on subsequent runs.
- Changed fingerprints require new proof. Missing checksums require rechecking
  on each run. Keep the ledger, including its `live_photo_pairs` table.
- gotohp currently supports attaching motion when the **still** already exists.
  A remote-video-only match is unsupported: the asset stays in iCloud.
- Dry-run does not modify the ledger or the destination. With deletion disabled,
  successful repair still records its evidence without deleting the source.

This feature does not restore media already removed from iCloud and does not
promise native Google Photos editable Portrait depth controls. Validation uses
synthetic cloud fixtures and subprocess-contract tests; no live-library repair
was performed as part of development.
