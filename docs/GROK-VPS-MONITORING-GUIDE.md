# Grok VPS Read-Only Monitoring Guide

**Updated:** 2026-07-27

**Scope:** Observe the Crypto Threshold VPS only. Do not operate the weather
project, deploy code, repair services, or change evidence.

**Authoritative project state:** Read `docs/PROJECT-STATUS.md` before every
check. If this guide conflicts with that file, stop and report the conflict.

## Prompt To Give Grok

```text
Read these files first:

1. /Users/xiafan/polymarket-crypto-threshold/docs/PROJECT-STATUS.md
2. /Users/xiafan/polymarket-crypto-threshold/docs/GROK-VPS-MONITORING-GUIDE.md

Then perform exactly one QUICK CHECK from the monitoring guide.

This is observation-only. Do not restart, stop, start, enable, disable, deploy,
edit, delete, migrate, back up, checkpoint, vacuum, reconcile, sign, or trade.
Do not read environment files, process environments, private keys, API secrets,
or wallet state. If a check fails, report UNKNOWN/FAIL with the exact evidence;
do not attempt a fix. Return the report using the guide's fixed template.
```

## Fixed Deployment Inventory

| Purpose | Value |
|---|---|
| VPS | `sean@38.76.191.251` |
| SSH key | `/Users/xiafan/.ssh/id_ed25519_polymarket_vps` |
| Application | `/opt/polymarket-crypto-threshold` |
| Runtime user | `crypto-threshold` |
| Daily service | `crypto-threshold-shadow.service` |
| Daily DB | `/opt/polymarket-crypto-threshold/data/phase2-vps.db` |
| Daily backup timer | `crypto-threshold-backup.timer` |
| Forward service | `crypto-threshold-forward-shadow.service` |
| Forward DB | `/opt/polymarket-crypto-threshold/data/phase2-forward.db` |
| Forward backup timer | `crypto-threshold-forward-backup.timer` |
| Up/Down service | `crypto-threshold-updown-shadow.service` |
| Up/Down DB | `/opt/polymarket-crypto-threshold/data/updown-shadow.db` |
| Up/Down backup timer | `crypto-threshold-updown-backup.timer` |
| Current deployment baseline | `4af0727` |

The daily service was a bounded 73-hour evidence run. It completed naturally
on 2026-07-27 at 14:40 CST after reporting 1,353 process-attributable cycles.
Its expected state is now `inactive/dead` with `Result=success`,
`ExecMainStatus=0`, and zero restarts. Never restart it. Report any later
active state as unexpected and hand it to the owner/Codex.

The forward daily-label collector started on 2026-07-27 at 16:47:32 CST from a
verified copy of the completed Daily evidence. It is bounded to 14 days, uses a
15-minute cadence, and should be `active/running` until it is explicitly stopped
after enough training and later OOS labels or reaches its bound. Its copied
history predates its service start by design.

The Up/Down service is intentionally continuous. Unless the owner explicitly
stopped it, it should remain `active/running`.

## Absolute Safety Boundary

Allowed operations:

- `date`, `timedatectl`, `cat` of `.deployed-commit`
- `systemctl show` and `systemctl list-timers`
- read-only `journalctl`
- read-only file listing
- SQLite opened with URI `mode=ro` plus `PRAGMA query_only=ON`

Forbidden operations:

- Any `systemctl start`, `stop`, `restart`, `enable`, `disable`, `reset-failed`,
  `daemon-reload`, or `edit`
- `kill`, `pkill`, `reboot`, package installation, or dependency sync
- `rm`, `mv`, `cp`, `install`, `chmod`, `chown`, `tee`, output redirection, or
  in-place editing
- `git pull`, `fetch`, `checkout`, `reset`, `rebase`, `merge`, or deployment
- Opening SQLite without `mode=ro`; any migration, `VACUUM`, checkpoint, or
  data update
- Running `doctor`, `phase2-acceptance`, settlement, replay, calibration, or
  backup commands unless the owner separately requests them
- Reading `/etc/polymarket-crypto-*.env`, `/proc/*/environ`, Keychain, private
  keys, funder data, API credentials, or authenticated endpoints
- Sending BUY/SELL, signing, cancellation, reconciliation, or account requests
- Deleting old local history

If a forbidden action seems necessary, stop. State what failed and hand the
decision back to the owner/Codex.

## Quick Check

Run from the local Mac:

```bash
cd /Users/xiafan/polymarket-crypto-threshold
ssh -i /Users/xiafan/.ssh/id_ed25519_polymarket_vps \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  sean@38.76.191.251
```

If SSH fails, report `UNKNOWN: SSH unavailable`. Do not claim any service is
down.

All commands below run inside the VPS SSH session.

### 1. Clock And Deployment

```bash
date --iso-8601=seconds --utc
timedatectl show --property=NTPSynchronized --value
cat /opt/polymarket-crypto-threshold/.deployed-commit
```

Expected:

- NTP prints `yes`.
- The deployment marker is `4af0727`, unless
  `docs/PROJECT-STATUS.md` records a newer reviewed deployment.
- `Observed at` in the final report must be the read-window end, not the time
  the SSH session or first command started.

Do not change a mismatched marker.

### 2. Service State

```bash
systemctl show \
  crypto-threshold-shadow.service \
  crypto-threshold-forward-shadow.service \
  crypto-threshold-updown-shadow.service \
  --property=Id,ActiveState,SubState,Result,MainPID,NRestarts,ExecMainStatus,ExecMainStartTimestamp,InactiveEnterTimestamp \
  --no-pager
```

Interpretation:

- Daily before its scheduled completion: `active/running` is PASS.
- Daily after its scheduled completion: `inactive/dead`, `Result=success`, and
  `ExecMainStatus=0` is `EXPECTED_COMPLETE`, not a reason to restart.
- Forward: `active/running` is PASS until its explicit early stop or 14-day
  bound. `inactive/dead` with success is expected only after one of those
  documented completion conditions.
- Up/Down: `active/running` is PASS.
- `failed`, non-zero exit, or any restart increase is FAIL.
- Current known restart baseline is zero for all three services.
- `ExecMainStartTimestamp` is the service process start. When a service is
  active, `InactiveEnterTimestamp` is historical systemd state and must not be
  treated as its current start or completion time.
- `.deployed-commit` identifies the filesystem tree, not code already loaded
  into a running Python process. If a service started before the marker was
  changed, report its loaded code version as `UNKNOWN` and do not say that the
  marker proves that process is running the new code.
- Compare each database's first `shadow_cycles.started_at` with that service's
  `ExecMainStartTimestamp`. If the database begins earlier, report
  `DB_HISTORY_PREDATES_SERVICE_START` and do not use the database minimum as
  the continuous service-window start without documented provenance.
- Forward is the documented exception: it was copied from the verified final
  Daily backup, so report `DERIVED_HISTORY_FROM_FINAL_BACKUP`. The completed
  73.06-hour segment remains historical evidence; do not describe the entire
  combined database span as one uninterrupted forward process.

### 3. Recent Logs

```bash
sudo journalctl \
  -u crypto-threshold-shadow.service \
  -u crypto-threshold-forward-shadow.service \
  -u crypto-threshold-updown-shadow.service \
  --since "2 hours ago" \
  --priority=err \
  --no-pager \
  -o short-iso
```

```bash
sudo journalctl \
  -u crypto-threshold-shadow.service \
  -n 20 \
  --no-pager \
  -o short-iso
```

```bash
sudo journalctl \
  -u crypto-threshold-forward-shadow.service \
  -n 20 \
  --no-pager \
  -o short-iso
```

```bash
sudo journalctl \
  -u crypto-threshold-updown-shadow.service \
  -n 20 \
  --no-pager \
  -o short-iso
```

`-- No entries --` in the error-priority query is PASS.

These log lines need careful interpretation:

- `complete_rest_fallback` is expected and healthy. REST is authoritative.
- `analyzed=14` means 14 workflows were attempted. It does not mean all 14
  signals have database status `analyzed`.
- `entered=N` means hypothetical paper-ledger entries only.
- A rejection is not automatically a service failure.
- Never interpret paper PnL as verified profitability.

### 4. Read-Only Database Snapshot

The VPS does not provide the `sqlite3` command. Use the project's Python
runtime exactly as shown:

```bash
sudo -u crypto-threshold \
  /opt/polymarket-crypto-threshold/.venv/bin/python - <<'PY'
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DATABASES = {
    "daily": Path(
        "/opt/polymarket-crypto-threshold/data/phase2-vps.db"
    ),
    "forward": Path(
        "/opt/polymarket-crypto-threshold/data/phase2-forward.db"
    ),
    "updown": Path(
        "/opt/polymarket-crypto-threshold/data/updown-shadow.db"
    ),
}
FORBIDDEN_TABLES = {
    "orders",
    "fills",
    "positions",
    "signers",
    "reconciliations",
    "order_intents",
    "trade_mutations",
}


def fetchall(connection: sqlite3.Connection, sql: str):
    return [tuple(row) for row in connection.execute(sql)]


def as_utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


for label, path in DATABASES.items():
    print(f"\n=== {label}: {path} ===")
    if not path.is_file():
        print("ERROR missing_database")
        continue

    try:
        read_started_at = datetime.now(UTC)
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        print("schema_version", connection.execute(
            "SELECT version FROM schema_meta"
        ).fetchone()[0])
        print("forbidden_tables", sorted(tables & FORBIDDEN_TABLES))

        cycle_summary = connection.execute(
            """
            SELECT COUNT(*), MIN(started_at), MAX(completed_at)
            FROM shadow_cycles
            """
        ).fetchone()
        print("cycle_summary", tuple(cycle_summary))
        print("cycle_statuses", fetchall(
            connection,
            """
            SELECT status, COUNT(*)
            FROM shadow_cycles
            GROUP BY status
            ORDER BY status
            """,
        ))
        recent_cycles = fetchall(
            connection,
            """
            SELECT status, discovered_count, analyzed_count,
                   paper_entered_count, reasons, completed_at
            FROM shadow_cycles
            ORDER BY completed_at DESC
            LIMIT 5
            """,
        )
        print("recent_cycles", recent_cycles)

        latest_completed = cycle_summary[2]
        if latest_completed:
            completed = datetime.fromisoformat(
                str(latest_completed).replace("Z", "+00:00")
            )
            age_seconds = (
                datetime.now(UTC) - completed.astimezone(UTC)
            ).total_seconds()
            print("latest_cycle_age_seconds", round(age_seconds, 1))

        print("signal_statuses", fetchall(
            connection,
            """
            SELECT status, COUNT(*)
            FROM analysis_signals
            GROUP BY status
            ORDER BY status
            """,
        ))
        print("paper_statuses", fetchall(
            connection,
            """
            SELECT status, action, COUNT(*)
            FROM paper_ledger
            GROUP BY status, action
            ORDER BY status, action
            """,
        ))
        print("settlement_labels", connection.execute(
            "SELECT COUNT(*) FROM settlement_labels"
        ).fetchone()[0])

        if label == "updown":
            if "settlement_attempts" not in tables:
                print("settlement_attempts", "MISSING")
            else:
                attempt_rows = fetchall(
                    connection,
                    """
                    SELECT market_id, last_status, attempt_count,
                           last_attempt_at, next_attempt_at, updated_at,
                           last_reason
                    FROM settlement_attempts
                    """,
                )
                retry_rows = [
                    row
                    for row in attempt_rows
                    if row[1] in {"pending", "error"}
                ]
                print("settlement_attempt_total", len(attempt_rows))
                print("settlement_attempt_statuses", fetchall(
                    connection,
                    """
                    SELECT last_status, COUNT(*)
                    FROM settlement_attempts
                    GROUP BY last_status
                    ORDER BY last_status
                    """,
                ))
                print("settlement_attempt_counts", fetchall(
                    connection,
                    """
                    SELECT last_status, attempt_count, COUNT(*)
                    FROM settlement_attempts
                    GROUP BY last_status, attempt_count
                    ORDER BY last_status, attempt_count
                    """,
                ))
                print("settlement_in_progress", [
                    (row[0], row[2], row[3], row[5], row[6])
                    for row in attempt_rows
                    if row[1] == "in_progress"
                ])
                resolution_payload_summary = fetchall(
                    connection,
                    """
                    SELECT payload_kind, COUNT(*), MAX(id), MAX(received_at)
                    FROM external_payloads
                    WHERE payload_kind = 'chainlink_resolution_event'
                    GROUP BY payload_kind
                    """,
                )
                print("resolution_payload_summary", resolution_payload_summary)
                print("top_resolution_payload_markets", fetchall(
                    connection,
                    """
                    SELECT market_id, COUNT(*), MAX(id), MAX(received_at)
                    FROM external_payloads
                    WHERE payload_kind = 'chainlink_resolution_event'
                    GROUP BY market_id
                    ORDER BY COUNT(*) DESC
                    LIMIT 5
                    """,
                ))
            print("latest_14_interval_status", fetchall(
                connection,
                """
                WITH latest AS (
                    SELECT rowid AS source_rowid, *
                    FROM analysis_signals
                    ORDER BY datetime(created_at) DESC, rowid DESC
                    LIMIT 14
                )
                SELECT r.candle_interval, l.status, COUNT(*)
                FROM latest AS l
                JOIN resolution_rules AS r
                  ON r.market_id = l.market_id
                GROUP BY r.candle_interval, l.status
                ORDER BY r.candle_interval, l.status
                """,
            ))
            boundary_mismatch = connection.execute(
                """
                SELECT
                    COUNT(*) AS signal_rows,
                    COUNT(DISTINCT s.market_id) AS market_count,
                    MAX(
                        CASE
                            WHEN ABS(CAST(l.strike AS REAL)) > 0.000000001
                            THEN ABS(
                                (
                                    CAST(s.threshold AS REAL)
                                    - CAST(l.strike AS REAL)
                                ) / CAST(l.strike AS REAL)
                            ) * 1000000.0
                            ELSE NULL
                        END
                    ) AS max_relative_ppm
                FROM analysis_signals AS s
                JOIN settlement_labels AS l
                  ON l.market_id = s.market_id
                WHERE ABS(
                    CAST(s.threshold AS REAL) - CAST(l.strike AS REAL)
                ) > 0.000000001
                """
            ).fetchone()
            boundary_distribution = fetchall(
                connection,
                """
                SELECT
                    s.asset,
                    r.candle_interval,
                    s.status,
                    COUNT(*) AS signal_rows,
                    COUNT(DISTINCT s.market_id) AS market_count
                FROM analysis_signals AS s
                JOIN settlement_labels AS l
                  ON l.market_id = s.market_id
                JOIN resolution_rules AS r
                  ON r.market_id = s.market_id
                WHERE ABS(
                    CAST(s.threshold AS REAL) - CAST(l.strike AS REAL)
                ) > 0.000000001
                GROUP BY s.asset, r.candle_interval, s.status
                ORDER BY s.asset, r.candle_interval, s.status
                """,
            )
            outcome_mismatch = connection.execute(
                """
                SELECT COUNT(*)
                FROM settlement_labels
                WHERE outcome_yes != CASE
                    WHEN exact_operator = '>='
                        THEN observed_value >= strike
                    WHEN exact_operator = '>'
                        THEN observed_value > strike
                    WHEN exact_operator = '<='
                        THEN observed_value <= strike
                    WHEN exact_operator = '<'
                        THEN observed_value < strike
                    ELSE -1
                END
                """
            ).fetchone()[0]
            print("boundary_mismatch", boundary_mismatch[0])
            print("boundary_mismatch_signal_rows", boundary_mismatch[0])
            print("boundary_mismatch_market_count", boundary_mismatch[1])
            print(
                "boundary_mismatch_max_relative_ppm",
                (
                    round(boundary_mismatch[2], 6)
                    if boundary_mismatch[2] is not None
                    else None
                ),
            )
            print("boundary_mismatch_distribution", boundary_distribution)
            print("outcome_mismatch", outcome_mismatch)
        read_finished_at = datetime.now(UTC)
        print("read_window", read_started_at.isoformat(), read_finished_at.isoformat())
        if label == "updown" and "settlement_attempts" in tables:
            due_count = sum(
                1
                for _, _, _, _, next_attempt_at, _, _ in retry_rows
                if next_attempt_at and as_utc(next_attempt_at) <= read_finished_at
            )
            print("settlement_attempt_due_count", due_count)
            in_progress_ages = []
            for row in attempt_rows:
                if row[1] != "in_progress":
                    continue
                attempted_at = as_utc(row[3])
                if attempted_at is not None:
                    in_progress_ages.append(
                        round(
                            (read_finished_at - attempted_at).total_seconds(),
                            1,
                        )
                    )
            print("settlement_in_progress_age_seconds", in_progress_ages)
            max_received_at = (
                resolution_payload_summary[0][3]
                if resolution_payload_summary
                else None
            )
            if max_received_at and as_utc(max_received_at) > read_finished_at:
                print(
                    "snapshot_consistency",
                    "INCOMPLETE: max_received_at_after_read_window",
                )
            else:
                print("snapshot_consistency", "PASS")
    except Exception as error:
        print("ERROR", type(error).__name__, str(error))
    finally:
        if "connection" in locals():
            connection.rollback()
            connection.close()
            del connection
PY
```

Freshness thresholds while a service is active:

| Database | PASS | WARN | FAIL |
|---|---:|---:|---:|
| Daily frozen source | 1,355 cycles unchanged | unit active without a new row | any cursor advance |
| Forward latest cycle | at most 25 minutes old | 25-40 minutes | over 40 minutes |
| Up/Down latest cycle | at most 5 minutes old | 5-10 minutes | over 10 minutes |

Additional interpretation:

- `forbidden_tables` must be `[]`. Match exact table names only.
  `analysis_signals` is valid and must not be rejected because it contains the
  substring `sign`.
- Up/Down `boundary_mismatch_signal_rows`,
  `boundary_mismatch_market_count`, and `outcome_mismatch` must all be zero.
  Repeated analysis of one market can produce many mismatched signal rows, so
  always report both signal-row and unique-market counts; do not describe the
  signal-row count as a market count. Also report the asset/interval/status
  distribution and maximum relative ppm. Any unique mismatch remains FAIL for
  the separate short-Up/Down replay path; do not weaken the exact comparison,
  relabel the sample, or merge this result into the daily evidence database.
- On schema v5, Up/Down must report `settlement_attempts` rather than
  `MISSING`. The due count includes only `pending` and `error` rows;
  succeeded rows are excluded because they should already have a label.
  Pending attempts are acceptable when their retry time is in the future; a
  growing `settlement_attempt_due_count` is WARN and needs owner review.
- Compare pending attempt counts by `attempt_count` across checks. Both
  attempt-1 rows and higher-attempt rows should advance over time. A stable
  process with no higher-attempt progress is a scheduler starvation WARN.
- `settlement_in_progress_age_seconds` must be reported. A single young
  `in_progress` row can be a read-time race; an `in_progress` row older than
  10 minutes, or one that persists across a permitted recheck, is WARN and
  needs owner review.
- `resolution_payload_summary` and `top_resolution_payload_markets` are
  cursors for comparing checks. Live books and ticks may grow the overall
  payload table. A market's resolution payload count or max ID should not
  advance merely because Gamma returned the same settlement meaning. Existing
  historical duplicates are evidence, not a reason to delete rows.
- `read_window` and `snapshot_consistency` are mandatory. If any
  `received_at` cursor is later than the read window end, label the report
  `SNAPSHOT_INCOMPLETE` inside the WARN verdict and do not use that cursor as
  a time-bounded claim. The read-only transaction is for consistency, not a
  write or checkpoint.
- After warm-up, the latest 14 Up/Down rows normally contain seven 5m and seven
  15m signals. A single stale/rejected asset is WARN, not proof of a broken
  service.
- If WARN is caused by freshness, a degraded cycle, an in-progress age, or a
  snapshot-consistency failure, wait five minutes and perform one read-only
  recheck. Structural WARNs such as a known historical partial file, a
  growing due backlog, or one isolated research rejection may be reported
  without a second check when the latest cycle is fresh; state why no recheck
  was needed. Do not loop indefinitely.
- Rejections such as missing start tick, incomplete book, stale current tick,
  or insufficient volatility history are fail-closed research outcomes.
- Repeated cycles with all 14 signals rejected after warm-up are WARN and need
  Codex review, even when the service process remains active.
- Forward began with 20 daily settlement labels after its first cycle. Report
  the current label count and delta from 20. Reaching 30 labels is only a
  training-data checkpoint; it is not Phase 2 acceptance and does not remove
  the need for a later OOS window, replay, calibration, and metrics.
- The frozen Daily baselines are 1,355 cycles, 134,030 external payloads, and
  10 settlement labels. Any increase is FAIL because that source is immutable.

### 5. Backup Timers And Files

```bash
systemctl show \
  crypto-threshold-backup.timer \
  crypto-threshold-forward-backup.timer \
  crypto-threshold-updown-backup.timer \
  --property=Id,ActiveState,SubState,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec \
  --no-pager
```

```bash
sudo -u crypto-threshold \
  ls -lht /opt/polymarket-crypto-threshold/backups
```

```bash
sudo -u crypto-threshold \
  ls -lht /opt/polymarket-crypto-threshold/backups/forward
```

```bash
sudo -u crypto-threshold \
  ls -lht /opt/polymarket-crypto-threshold/backups/updown
```

```bash
sudo -u crypto-threshold sh -c \
  'cd /opt/polymarket-crypto-threshold && \
   find backups -maxdepth 2 -type f -name "*.partial*" -print'
```

Expected:

- The completed Daily timer is expected to be `inactive/dead` and disabled
  after its final backup. Forward and Up/Down timers must be `active/waiting`
  and enabled.
- The final `find` command prints nothing.
- A `.partial`, `.partial-wal`, or `.partial-shm` file is WARN. Report the exact
  absolute path, distinguish daily root from `backups/updown`, and do not
  delete it.
- A missing scheduled Forward or Up/Down backup, after its first scheduled
  trigger, or a backup older than 26 hours is WARN.
- Report each backup's age at the read-window end and the 26-hour threshold.
  Do not estimate age from a filename while using an earlier observation time.

Do not manually start a backup service. Backup creation is a mutation and is
reserved for the owner/Codex.

## Verdict Rules

Use one overall verdict:

- `PASS`: SSH works, NTP is synchronized, expected services are healthy,
  evidence is fresh, no error logs, no forbidden tables, and backup timers are
  healthy.
- `WARN`: A transient rejection/degraded cycle, stale asset, delayed cycle,
  stale backup, partial file, deployment mismatch, history-before-service
  mismatch, `SNAPSHOT_INCOMPLETE`, or unexpected metric needs review but
  evidence is still readable.
- `FAIL`: A required service failed, active-service evidence is beyond the FAIL
  threshold, NTP is not synchronized, a DB is missing/unreadable, a forbidden
  table exists, boundary/outcome mismatch is non-zero, or logs show an
  unhandled exception.
- `UNKNOWN`: SSH or permissions prevented observation. Never convert UNKNOWN
  into PASS or FAIL by guessing.
- `EXPECTED_COMPLETE`: The bounded Daily service ended successfully and its
  final backup was verified. This status does not override a Forward or
  Up/Down failure.

No monitoring result may declare:

- Phase 2 accepted
- The system profitable
- Live trading safe
- Paper PnL equivalent to real PnL
- An order, fill, or position confirmed

## Fixed Report Template

```text
Crypto VPS Read-Only Monitor

Observed at UTC:
Observed at Asia/Shanghai:
Overall verdict: PASS | WARN | FAIL | UNKNOWN | EXPECTED_COMPLETE

Access
- SSH: PASS/FAIL
- NTP synchronized: yes/no/unknown
- Deployed marker:

Daily service
- ActiveState/SubState:
- Result/ExecMainStatus:
- MainPID:
- NRestarts:
- ExecMainStartTimestamp:
- InactiveEnterTimestamp (informational only while active):
- Filesystem marker vs loaded process:
- DB schema:
- Cycle count and time range:
- DB first cycle vs service start:
- Latest cycle age:
- Expected completion / remaining time:
- Last 5 cycle statuses:
- Signal status counts:

Forward service
- ActiveState/SubState:
- Result/ExecMainStatus:
- MainPID:
- NRestarts:
- ExecMainStartTimestamp:
- Filesystem marker vs loaded process:
- DB schema:
- Cycle count and time range:
- Derived-history provenance:
- Latest cycle age:
- Expected completion / remaining time:
- Last 5 cycle statuses:
- Signal status counts:
- Settlement label count / delta from 20:

Up/Down service
- ActiveState/SubState:
- Result/ExecMainStatus:
- MainPID:
- NRestarts:
- ExecMainStartTimestamp:
- Filesystem marker vs loaded process:
- DB schema:
- Cycle count and time range:
- DB first cycle vs service start:
- Latest cycle age:
- Last 5 cycle statuses:
- Latest 14 interval/status:
- Signal status counts:
- Paper status counts:
- Settlement label count:
- Settlement attempt statuses:
- Pending/error attempt due count:
- Attempt-count distribution:
- In-progress rows and age:
- Resolution payload summary/cursor:
- Read window and snapshot consistency:
- Boundary mismatch signal rows / unique markets:
- Boundary mismatch distribution / maximum relative ppm:
- Outcome mismatch:

Backups
- Daily timer:
- Forward timer:
- Up/Down timer:
- Latest backup timestamps:
- Backup age at read-window end / threshold:
- Partial files:

Errors and anomalies
- Exact evidence:
- First observed:
- One permitted recheck result:

Safety attestation
- Commands performed were read-only: yes/no
- Service or file mutation performed: no
- Real trading/authenticated mutation performed: no
- Secrets read or printed: no

Recommended next owner action:
- NONE, or one concise escalation request
```

Do not omit failed commands. Include their command, exit code, and exact error.
Do not paste huge logs; include the relevant 10-30 lines and their timestamps.

## Escalation Handoff

Escalate to the owner/Codex without attempting repair when any of these occurs:

- Completed Daily service becomes active or its database gains new rows
- Forward service ends before an explicit stop or its bound, exits non-zero,
  or restarts
- Up/Down service is not active
- Up/Down service restarts
- Three consecutive cycles are degraded or fail
- Evidence freshness crosses a FAIL threshold
- Both 5m and 15m remain fully rejected after warm-up and one recheck
- NTP changes from `yes`
- A forbidden table, mismatch, corruption, traceback, OOM, or disk-full message
  appears
- A required Forward/UpDown timer is disabled or its scheduled backup is
  missing
- Forward reaches at least 30 labels so the owner/Codex can freeze the training
  boundary and define the later OOS window
