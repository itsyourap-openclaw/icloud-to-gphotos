# Select an iCloud library and albums

Use `i2g libraries` to list accessible library IDs/scopes as JSON. Set
`I2G_ICLOUD_LIBRARY` to an exact ID, then use `i2g albums` to discover album IDs
and full folder paths. Discovery reads metadata only and acquires the normal
migration lock; it does not upload or delete photos.

Examples:

```dotenv
I2G_ICLOUD_LIBRARY=root
I2G_INCLUDE_ALBUMS=["album-id-1","Travel/Trip"]
I2G_EXCLUDE_ALBUMS=["private-album-id"]
```

Include lists are a union; exclusions win. Empty includes mean all photos minus
exclusions. Selectors resolve by ID, then exact full path, then unambiguous name.
Unknown/ambiguous selectors fail rather than widening the migration. Folder
containers must be expanded into individual albums explicitly. Duplicate album
memberships are processed once. Hidden and deleted items remain excluded even
when a selected album contains them. Exclusion enumeration completes before any
selected asset is yielded, so a partial exclusion response cannot leak assets
into migration.

## Shared Photo Libraries

```dotenv
I2G_ICLOUD_LIBRARY=shared:the-exact-zone-from-libraries
I2G_DELETE_FROM_ICLOUD=false
```

All explicit non-root libraries are **backup-only**. Configuration rejects
source deletion; the pipeline and CloudKit adapter also block it independently.
Legacy shared streams (`shared`, without a zone) are not supported by this
pipeline. An unavailable selected library never silently falls back to root.

## Ledger isolation and compatibility

Selection is opt-in. With all selection settings unset, the previous personal
library behavior and `<state_dir>/ledger.db` remain unchanged.

Setting a library or any album selector activates a separate ledger at
`<state_dir>/sources/<account-and-library-hash>/ledger.db`. It is shared across
album filters for the same source, but not across accounts or libraries. This
prevents a same-ID asset from borrowing another source's upload confirmation.
The legacy ledger is retained unmodified; it is not automatically copied into a
new source scope because its account/library provenance is unknown. On first
use, retained media is rechecked/deduplicated with Google Photos, and first-seen
grace periods may restart if import dates are unavailable. Back up all ledgers.

The run report's library-remaining count describes the whole selected library,
not just the album filter. All behavior was tested with synthetic libraries;
Shared Library compatibility has not been exercised on a live account.
