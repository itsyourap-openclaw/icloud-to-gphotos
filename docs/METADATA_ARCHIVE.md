# Durable iCloud metadata archive

Set `I2G_METADATA_ARCHIVE_DIR` to a permanent directory (for example,
`/var/lib/icloud-to-gphotos/metadata-archive`). Unset leaves this feature disabled.
Do not choose staging, the rotated log/report directories, or a temporary disk.
Back up the archive and ledger together; archive files are not sent to Google Photos.

Each asset gets account/library-scoped, hashed directories containing immutable
revisions. A revision contains `manifest.json` and `metadata.xmp`; `latest.json`
identifies the most recently observed revision. Changed metadata creates a new
revision; identical metadata reuses its existing revision after verifying it.

The JSON records captions/descriptions, keywords, favorites, hidden status,
capture/import dates, available GPS fields, user album IDs and paths, adjustment
status/type, and available/selected resource manifests. XMP generation uses
pyicloud's existing materialization helpers. Smart albums and folder containers
are not copied as album memberships. Original IDs and filenames in JSON let you
join the archive to the migration ledger. Signed download URLs, cookies, tokens,
and complete raw CloudKit records are deliberately not copied.

Archives are written before source deletion, including for media uploaded on an
earlier run. Album enumeration must complete successfully. A failed write,
missing metadata, or corrupt existing revision blocks deletion and is reported
as partial progress; uploading may still succeed and will not need repeating
once archiving works. Dry-run writes no archive. Temporary files are staged in
the archive filesystem and revisions are renamed atomically after file fsync;
on POSIX, parent directories are fsynced too. Interrupted `.pending-*` directories
are not committed revisions and can be removed when no migration is running.

This is a descriptive metadata archive, not an export of Apple's complete
non-destructive adjustment recipe, face-recognition database, or editable depth
controls. Google Photos may not import these sidecar fields automatically.
Archive contents themselves are private photo metadata; directory/file modes
are restricted where the operating system supports them.
