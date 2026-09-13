"""Cross-process exclusion for migration state and shared staging volumes."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from filelock import FileLock, Timeout

from .config import Settings


class MigrationBusy(RuntimeError):
    """Another migration owns this state or staging directory."""


@contextmanager
def migration_lock(settings: Settings) -> Iterator[None]:
    """Fail immediately if a migration or dry-run is already active.

    Kernel-backed locks are released if a process crashes. Never unlink a
    lockfile: waiters may still hold its inode. Lock both directories because
    separate state directories can be configured to share a staging directory.
    """
    assert settings.staging_dir is not None
    paths = {
        settings.state_dir.resolve() / ".migration.lock",
        settings.staging_dir.resolve() / ".migration.lock",
    }
    with ExitStack() as stack:
        for path in sorted(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                stack.enter_context(FileLock(path, timeout=0))
            except Timeout as exc:
                raise MigrationBusy(
                    "Another migration is using this state or staging directory; retry later."
                ) from exc
        yield
