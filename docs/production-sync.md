# Production Source Synchronization

Snapshot date: 2026-10-28 (Europe/Moscow).

The default GitHub branch was at `9ca49ae`, containing the original calculator.
The running bot uses image
`0899fdac247d19f8e14180a025958ea78effa2fa5ae8c8b701fe72026acbdf50`
and Alembic revision `b369c217f458`.

## Scope

Imported the application's source, migrations, tests, locale catalogs, build
configuration, operational tooling and general design documentation. All 131
files under `bot/` match the production source snapshot after normalizing text
line endings. This import does not change the server or its database.

Excluded runtime env files, credentials, database backups, exchange CSV exports,
local deployment overrides, temporary patches and per-user financial audits.
Private deployment/financial audit appendices were also removed from the
public feature documentation; general behavior descriptions are retained.
The source export was checked against the live containers' credential values;
no matches were found. Known live identifiers in test examples and historical
operational plans were replaced with synthetic identifiers or documentation IPs.

Repository-only changes are intentionally outside `bot/`: stronger ignore rules,
a Docker build-context denylist, a missing test dependency, refreshed lockfile,
this documentation and deterministic clocks for three old date-sensitive tests.
CI also receives explicit dummy Telegram settings and an unreachable fallback
database URL; actual database tests provision their own disposable PostgreSQL.

## Validation

The production snapshot previously passed 964 tests with three old date-sensitive
tests deselected. Those three tests now use fixed clocks consistent with their
existing July 2026 fixtures; no business rules were changed to make tests pass.

The snapshot has pre-existing full-repository Ruff and mypy findings. They must
not be concealed by weakening the checks. Treat this source-restoration change
as a draft until static-analysis debt and the release process are reviewed.

The imported tree passed all **967 tests** in a Python 3.11 container with isolated
PostgreSQL, with no tests deselected. There are 41 existing warnings. The lockfile
passes `uv lock --check`. Full Ruff reports 34 pre-existing findings; runtime
source is intentionally retained rather than silently refactored during import.
Full mypy in the Python 3.11 verification image reports 23 existing errors in
nine files. These are recorded as validation debt, not described as passing.

## Deployment Boundary

The server source directory is not a Git checkout. The existing GitHub deploy
workflow assumes a Git checkout and recreates the Compose stack. Do not merge
and trigger that deployment without first reconciling its target directory and
the desired release procedure. This synchronization is pushed on a non-default
branch so it cannot satisfy the workflow's main/master deployment condition.

The accrual and executor containers currently use older immutable images.
They are not restarted by this repository synchronization. Future deployment
must explicitly review all three process versions, secret distribution, schema
compatibility and rollback; importing source is not a deployment authorization.
