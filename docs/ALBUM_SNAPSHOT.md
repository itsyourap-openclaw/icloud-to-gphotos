# Selected-album traversal

Album-filtered migrations finish listing each source album before processing
its members. Deleting earlier batches therefore cannot shift CloudKit's live
page offsets and silently skip the remaining members. A listing failure prevents
any member of that album from being processed on that traversal.

Exact selector resolution, exclusion precedence, hidden/deleted filtering and
cross-album deduplication are unchanged. The snapshot buffers metadata objects
for one album at a time; media downloads still obey batch/disk limits. Very large
individual albums therefore need memory for their metadata listing. This change
does not replace the normal unfiltered library traversal or enable incremental
lookup for filtered runs.
