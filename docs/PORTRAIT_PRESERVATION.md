# Portrait effects and current renditions

## Why the original can look flat

An unmodified original is not necessarily the image displayed by Apple Photos.
Portrait blur/lighting, crops and filters can be adjustments applied to that
original. Preserving the original container and preserving the visible result
are different requirements.

| CloudKit resource | Meaning for this pipeline |
| --- | --- |
| `CPLMaster.resOriginal*` | Unmodified original, including any embedded auxiliary data |
| `CPLMaster.resJPEGFull*` | Master preview; not proof of the current edited appearance |
| `CPLAsset.resJPEGFull*` | Current full-size rendered image |

The previous planner read `resJPEGFull` from the master record. Its fixtures
also put edited resources on the master, so tests did not detect the error.
The asset-level location of Portrait renditions is documented in the
[upstream iCloud downloader investigation](https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/249#issuecomment-739532739).

The planner now reads the asset's token, file type, fingerprint and dimensions
together. It does not mix them with the master preview or fall back to that
preview when the current render is missing. Despite the `JPEG` prefix, the
render can be HEIC or HEIF; its declared format controls the filename. A missing
file-type field defaults to JPEG. An asset-level render is preserved even when
the adjustment marker is absent. A master preview alone does not imply an edit.

## Policy and deletion

| `I2G_EDITED_POLICY` | Uploaded images | Trade-off |
| --- | --- | --- |
| `both` (default) | Original and current render, as separate Google Photos items | Keeps the original plus the applied appearance |
| `edited` | Current render when available | Does not retain the unmodified still or guarantee its auxiliary data |
| `original` | Original only | Explicitly omits the current rendering; visible effects may be absent |

For `both` and `edited`, a missing current render produces a partial-run report
and prevents deletion. The original is still backed up if available. A later
run retries the render without downloading an already-confirmed original.
The render must have its own successful upload or remote-duplicate verdict.
Original and rendered stills are not paired together as a Live Photo.
Under `both`, a missing original also prevents deletion: a rendered image
cannot replace the original container's auxiliary data.

On upgrading a version-1 ledger, legacy `edited` confirmations and exhausted
retries for retained assets are reset once. They may refer to flat master
previews, even if filenames and sizes match. Original confirmations and
historical `purged` rows are left intact. Keep the existing ledger: there is no
need to clear all migration progress. New fingerprints, changed known sizes or
changed filenames also invalidate a resource's previous confirmation.

## What this does not prove

A rendered JPEG preserves visible blur but is not an editable depth map.
Keeping the original preserves whatever auxiliary data iCloud returns; neither
that nor a successful upload proves Google Photos will expose Apple's depth
adjustments, edit history or the same HDR interpretation as its native iOS app.
This pipeline does not translate Apple depth data into Google's editing model.
It downloads the chosen files without transcoding; the
[pinned uploader streams those files](https://github.com/xob0t/gotohp/blob/20583b8815255f37cec459d486147371281c1e0e/backend/api.go#L367).
Optional metadata backfill may still add missing date/GPS tags. Set
`I2G_BACKFILL_METADATA=false` when byte-exact container transfer is required.

Automated tests cover resource selection, typed CloudKit tokens, byte transport,
retry/deletion gates and ledger upgrades. They do **not** certify native Google
Photos depth-editing support. No private-library upload is part of those tests.

To validate actual library content before allowing deletion, use `both` and run
a bounded upload with deletion disabled:

```sh
uv run i2g run --no-delete --max-batches 1
```

Inspect the rendered item's appearance and, separately, any depth controls you
require. Keep deletion disabled until those requirements are verified. A
previously uploaded flat image cannot recreate a lost edit; already-deleted
assets are not automatically recovered or repaired by this change.
