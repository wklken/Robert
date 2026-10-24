# Upgrade to the SQLite execution state machine

Robert now uses one SQLite-backed execution path. DBOS and PostgreSQL are no
longer runtime dependencies. Task, Attempt, Workstream, Episode, audit and
publication IDs remain unchanged. Historical workflow tables and migration
checksums are retained. This procedure does not delete the DBOS system database.

This is an offline handoff, not an automatic engine switch. Keep the old package,
its environment and DBOS history available until the handoff has been accepted.
Never run old and new writers against the same data concurrently.

## Prepare a side-by-side executable

Build the reviewed revision, then install its wheel into a **stable** environment
outside the checkout. Service installation records the executable path; do not
install the service from a temporary smoke-test environment.

```bash
python3 -m build
python3 -m venv "$HOME/.local/share/robert/sqlite-runtime"
"$HOME/.local/share/robert/sqlite-runtime/bin/python" -m pip install dist/*.whl
ROBERT_NEW="$HOME/.local/share/robert/sqlite-runtime/bin/robert"
ROBERT_OLD="$(command -v robert)"
ROBERT_DB="$HOME/.local/share/robert/robert.sqlite3"
ROBERT_CONFIG="$HOME/.config/robert/config.yml"
"$ROBERT_NEW" migrate dbos --help
"$ROBERT_NEW" migrate dbos --db "$ROBERT_DB" --dry-run --output json
```

Use the actual deployed database/config paths, not the defaults above when your
installation differs. An initial dry-run can be blocked while the old service
is running; that is inventory, not permission to apply. Preview opens the
existing database read-only and never starts a Worker or contacts GitHub.
`--plan-file` creates a new private file; it refuses to overwrite an existing
plan. Keep plans and backups private because they can contain task context.

## Drain old execution and stop writers

Pause incoming assignments, operator retries and any automation that starts new
work. Keep the old implementation supervising existing Workers until they finish
and their results/publications are confirmed. Waiting-for-user or CI tasks need
not wait indefinitely: they may transfer at a verified idle boundary. There is no
new automatic `drain` or `force` command in this release.

Stop the old service and prevent its service manager, cron or other automation
from restarting it. Stop separate Web writers, `run once`, publishers and result
producers as well. Verify the process identities and service definitions; a lease
expiry is not proof that an old process has stopped.

```bash
"$ROBERT_OLD" service status
"$ROBERT_OLD" service stop
"$ROBERT_OLD" service uninstall
```

Record the original service configuration before uninstalling it. Custom service
managers must be stopped/disabled through their own configured mechanism. Do not
kill an unidentified Worker just to make a preflight pass.

The tool blocks live/unknown Worker identity (including completed Attempts whose
process still exists), in-flight dispatch/publication, active leases, pending
result spools, running daemon/agent-run records, unsupported active v1 workflows,
missing events and inconsistent controls. Resolve each cause using the old
version and its evidence, then generate a new plan. `attention` remains stopped;
spent recovery and retry budgets are preserved.

**Crashed historical processes:** old run records do not contain sufficient PID
identity for automatic retirement. A stale `running` row or active lease can
therefore remain a blocker after `service stop`. This release deliberately has
no SQL reset/force bypass. An operator must establish process death and arrange
an explicitly reviewed historical-state disposition with the original tooling.
An expired timestamp alone is insufficient. Do not treat blocked preflight as
successful migration.

## Back up and rehearse

After all writers and Workers are quiescent, preserve the old binary/environment,
configuration, full results spool directory (`DATABASE.results`), worktrees and
artifacts. Preserve DBOS system-database history using its existing backup
procedure and retain any required old driver/version for unsupported subjects.
Use SQLite's backup API so committed WAL content is included:

```bash
set -eu
ROBERT_BACKUP_DIR="$(mktemp -d "$HOME/robert-sqlite-upgrade-backup.XXXXXX")"
chmod 700 "$ROBERT_BACKUP_DIR"
python3 - "$ROBERT_DB" "$ROBERT_BACKUP_DIR/robert.sqlite3" <<'PY'
import sqlite3, sys
from contextlib import closing
from pathlib import Path
source = Path(sys.argv[1]).expanduser().resolve(strict=True)
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
    with closing(sqlite3.connect(sys.argv[2])) as dst:
        src.backup(dst)
PY
cp -p "$ROBERT_CONFIG" "$ROBERT_BACKUP_DIR/config.yml"
if [ -d "${ROBERT_DB}.results" ]; then
  cp -Rp "${ROBERT_DB}.results" "$ROBERT_BACKUP_DIR/robert.sqlite3.results"
fi
# Copy relevant worktrees/artifacts and the old service definition to this
# backup using your deployment's actual paths.
```

Make a separate rehearsal copy of the backup database. Run preview/apply on that
copy, then compare task/attempt/result/action IDs, states, audit evidence and
spent budgets. Plans are path-bound: generate a fresh plan for each copy/target.

```bash
cp -p "$ROBERT_BACKUP_DIR/robert.sqlite3" "$ROBERT_BACKUP_DIR/rehearsal.sqlite3"
if [ -d "$ROBERT_BACKUP_DIR/robert.sqlite3.results" ]; then
  cp -Rp "$ROBERT_BACKUP_DIR/robert.sqlite3.results" "$ROBERT_BACKUP_DIR/rehearsal.sqlite3.results"
fi
"$ROBERT_NEW" migrate dbos --db "$ROBERT_BACKUP_DIR/rehearsal.sqlite3" \
  --dry-run --plan-file "$ROBERT_BACKUP_DIR/rehearsal-plan.json" --output json
"$ROBERT_NEW" migrate dbos --db "$ROBERT_BACKUP_DIR/rehearsal.sqlite3" \
  --apply --plan-file "$ROBERT_BACKUP_DIR/rehearsal-plan.json" --output json
```

Rehearsal here means **migration only**. Copied SQLite rows retain absolute
repository, worktree, artifact and configuration paths. Never start a daemon or
`run once` on a copy unless every related path and external destination has also
been isolated. Do not allow rehearsal to write to production repositories.

## Apply to the deployment

Keep all writers stopped. Generate a fresh plan against the actual target and
inspect every blocker and mapping before apply:

```bash
"$ROBERT_NEW" migrate dbos --db "$ROBERT_DB" --dry-run \
  --plan-file "$ROBERT_BACKUP_DIR/deployment-plan.json" --output json
"$ROBERT_NEW" migrate dbos --db "$ROBERT_DB" --apply \
  --plan-file "$ROBERT_BACKUP_DIR/deployment-plan.json" --output json
"$ROBERT_NEW" migrate dbos --db "$ROBERT_DB" --dry-run --output json
```

Apply rechecks the fingerprint and acquires exclusive database/publication/
dispatch locks. A stale plan is refused. Reapplying the original successful plan
returns `already_applied`; a fresh preview/apply returns `already_migrated`.
A database with no DBOS work can return `not_needed`.

The journal retains original DBOS bindings and signal mappings. Old DBOS bindings
are retired without canceling/completing the business Task. Unconsumed signals
retain their payload/topic/dedupe identity in SQLite; delivery alone is never
accepted as proof that the business event was consumed.

Remove the old `workflow_engine` block or set `mode: sqlite`. The old
`mode: legacy` input is accepted as a deprecated alias for SQLite; `active` and
`shadow` are rejected with migration guidance. DBOS connection environment
variables are no longer needed by the new runtime. Keep them with the offline
old environment only while historical recovery/backup requires it.

```bash
"$ROBERT_NEW" config validate --config "$ROBERT_CONFIG"
"$ROBERT_NEW" doctor --config "$ROBERT_CONFIG" --skip-external
export PATH="$HOME/.local/share/robert/sqlite-runtime/bin:$PATH"
"$ROBERT_NEW" service install --config "$ROBERT_CONFIG"
"$ROBERT_NEW" service start
"$ROBERT_NEW" service status
"$ROBERT_NEW" status --config "$ROBERT_CONFIG" --output json
```

The PATH change makes service installation resolve the new executable. Use the
actual stable environment path if you chose a different location.
Check `--help` for deployment-specific command options. Confirm that the service
uses the new stable executable, that no old process restarted, and that every
nonterminal task is either progressing or explicitly waiting/stopped with a
cause. Check task/attempt/action IDs, pending facts, publication reconciliation,
spool ingestion and notification delivery. Only then restore incoming work.
Production rollout remains an operator action; tests do not perform it.

## Rollback boundary

Before the new path performs external effects, stop all new writers and restore
the matched backup set (business DB, spool, worktrees/config and old runtime
history) under a reviewed rollback. Restore the old service only after verifying
its ownership and process state.

After any new Worker or GitHub/Git effect, **do not restore a stale SQLite file**.
That would forget new facts and could repeat work or publication. Stop execution,
retain all evidence, reconcile remote/process effects, then repair forward or
perform a separately reviewed state reconciliation. Historical tables/checksums
are retained; deleting them is a later maintenance decision.
