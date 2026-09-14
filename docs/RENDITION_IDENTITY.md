# Rendition identity and retries

Upload confirmations are reusable only while the current resource has a matching
nonempty iCloud checksum, size (when available), and filename. A same-size render
without a current checksum is not evidence that the bytes are unchanged.

On observing a checksumless confirmed resource again, the pipeline discards its
old upload proof and downloads/reconfirms that resource. This includes older
ledger entries; no ledger deletion or manual migration is needed. Known unchanged
originals retain their confirmation. Missing current checksums are recorded as
missing rather than replaced with historical values.

Checksumless failed resources keep their attempt counts and exhaustion state.
The safety check does not turn permanent failures into unlimited retries.
Checksumless backups may require repeated transfers on later full scans; this is
the intentional cost of not deleting an unverified newer rendition. Incremental
scanning still discovers changed resources through CloudKit events.
