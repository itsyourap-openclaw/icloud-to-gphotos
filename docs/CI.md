# Continuous integration

[The CI workflow](../.github/workflows/ci.yml) runs on every push (branches and
tags), on pull requests, and through a manual `workflow_dispatch`. There are no
branch or path filters, so changes to tests, dependencies, deployment files, and
documentation receive the same checks. Pull request runs test GitHub's proposed
merge with the base branch; push runs test the pushed revision.

## Checks

| Check | What it verifies |
| --- | --- |
| `Code quality` | `uv sync --locked --dev`, Ruff on `src` and `tests`, and mypy on `src`, using Python 3.11 |
| `Tests (Python 3.11)` | Full pytest suite, branch coverage, and native metadata integration tests |
| `Tests (Python 3.12)` | The same suite on Python 3.12 |
| `Tests (Python 3.13)` | The same suite on Python 3.13 |

The matrix matches `requires-python` in `pyproject.toml`. Update both together
when the supported Python range changes. Jobs use Ubuntu 24.04, the committed
`uv.lock`, and a pinned uv version. `--locked` fails if dependency declarations
and the lockfile disagree; CI never silently regenerates the lockfile.

Each test job installs ExifTool and FFmpeg (including ffprobe) and checks that
the binaries are discoverable. This keeps the JPEG and video metadata tests
from being silently skipped due to missing system tools. Cloud operations use
test fixtures and fake uploader processes: no iCloud or Google account, gotohp
build, real photo library, or application secrets are needed.

JUnit and coverage XML reports are uploaded as `test-results-py<version>`
artifacts and retained for 14 days. Reports are uploaded even when pytest fails,
provided the run was not cancelled and pytest produced them. The existing
terminal coverage report is also retained in the job log; no new coverage
threshold is imposed.

## Reproduce locally

On Ubuntu, install `libimage-exiftool-perl` and `ffmpeg`. Then, from the checkout:

```bash
uv sync --locked --dev --python 3.11
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
uv run --no-sync pytest --junitxml=test-results/junit.xml --cov-report=xml:test-results/coverage.xml
```

Repeat sync and pytest with Python 3.12 and 3.13 to reproduce the matrix. Generated
reports are ignored by Git. Without the native tools, local metadata integration
tests still skip as before. Workflow syntax can additionally be checked with
[actionlint](https://github.com/rhysd/actionlint):

```bash
actionlint .github/workflows/ci.yml
```

## Permissions and maintenance

CI has only `contents: read`, does not persist checkout credentials, and uses
ordinary `pull_request` events rather than `pull_request_target`. No repository
secrets need to be configured. GitHub may require a maintainer to approve a
fork contributor's first run; the workflow does not bypass that policy.

All actions are pinned to full commit SHAs with release-version comments.
Keep the SHA and its comment in sync when updating an action. The uv version
is defined once at the top of the workflow. Dependency caching is keyed by the
lockfile. Newer runs cancel older runs for the same event and branch/PR, while
push and PR runs do not cancel each other. Matrix failures do not cancel the
other Python versions.

After merging, maintainers can select the four check names above as required
checks in the upstream repository's branch rules. Adding this workflow does not
change branch protection, merge PRs, deploy the application, or publish packages.
