"""Bind backup evidence to gotohp's effective destination without logging credentials."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .uploader import UploadError

_ACTIVE = re.compile(r'^\s+\*\s+([^\s@]+@[^\s@]+)\s*$', re.MULTILINE)


def _probe(binary: Path, config: Path | None) -> subprocess.CompletedProcess[str]:
    command = [str(binary), 'creds', 'list']
    if config is not None:
        command += ['--config', str(config)]
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        # Raw command output/errors may include accounts or authentication material.
        raise UploadError('Cannot inspect the effective gotohp destination account.') from None


def selected_account(binary: Path, config: Path | None) -> str:
    """Read exactly one marked active account; merely listing credentials is insufficient."""
    result = _probe(binary, config)
    accounts = _ACTIVE.findall(result.stdout)
    if result.returncode or len(accounts) != 1:
        raise UploadError('gotohp must report exactly one active destination account.')
    return accounts[0].strip().casefold()


def account_identity(account: str, config: Path | None) -> str:
    """Opaque binding; changing even an equivalent explicit config requires a new scope."""
    values = ['gotohp-destination-v1', account.strip().casefold(),
              str(config.resolve()) if config is not None else None]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class DestinationGuard:
    """Detect account changes around remote operations; never infer from a config filename."""

    def __init__(self, binary: Path, config: Path | None) -> None:
        self.binary, self.config = binary, config
        if config is not None:
            if not config.is_file():
                raise UploadError('The configured gotohp configuration file does not exist.')
            # The pinned gotohp can silently ignore --config. An empty probe must
            # not expose its default account, or the requested config is untrustworthy.
            with tempfile.TemporaryDirectory(prefix='i2g-config-probe-') as directory:
                blank = Path(directory) / 'empty.config'
                blank.write_text('{}\n', encoding='utf-8')
                if _ACTIVE.findall(_probe(binary, blank).stdout):
                    raise UploadError('gotohp ignores --config; use its active default/portable '
                                      'configuration with I2G_GOTOHP_CONFIG unset, or a build '
                                      'that honors explicit configuration.')
        self.identity = account_identity(selected_account(binary, config), config)

    def check(self) -> None:
        current = selected_account(self.binary, self.config)
        if account_identity(current, self.config) != self.identity:
            raise UploadError('The effective gotohp destination changed during this run; '
                              'upload evidence was not accepted. Restore the original account.')
