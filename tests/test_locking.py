"""Exercise real OS locks, including competing processes and crash release."""

import subprocess
import sys

import pytest

from icloud_to_gphotos.locking import MigrationBusy, migration_lock


def test_lock_blocks_a_second_process_and_releases_on_exit(settings):
    script = """
import sys
from filelock import FileLock, Timeout
try:
    with FileLock(sys.argv[1], timeout=0):
        pass
except Timeout:
    sys.exit(7)
"""
    command = [sys.executable, "-c", script, str(settings.state_dir / ".migration.lock")]
    with migration_lock(settings):
        assert subprocess.run(command, check=False).returncode == 7
    assert subprocess.run(command, check=False).returncode == 0


def test_shared_staging_is_locked_across_different_state_directories(settings, tmp_path):
    other = settings.model_copy(update={"state_dir": tmp_path / "other-state"})
    with migration_lock(settings), pytest.raises(MigrationBusy), migration_lock(other):
        pytest.fail("overlapping staging admitted")
    with migration_lock(other):
        pass


def test_crashed_process_does_not_leave_a_stale_lock(settings):
    script = """
import os, sys
from filelock import FileLock
lock = FileLock(sys.argv[1], timeout=0)
lock.acquire()
os._exit(9)
"""
    command = [sys.executable, "-c", script, str(settings.state_dir / ".migration.lock")]
    assert subprocess.run(command, check=False).returncode == 9
    with migration_lock(settings):
        pass
