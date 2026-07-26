# Grok VPS Read-Only Monitoring Guide

**Updated:** 2026-07-26

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
| Up/Down service | `crypto-threshold-updown-shadow.service` |
| Up/Down DB | `/opt/polymarket-crypto-threshold/data/updown-shadow.db` |
| Up/Down backup timer | `crypto-threshold-updown-backup.timer` |
| Current deployment baseline | `b8e69d2` |

The daily service is a bounded 73-hour evidence run. It began on
2026-07-24 and is expected to exit normally around 2026-07-27 14:40 CST.
After that time, `inactive/dead` with `Result=success` can be expected
completion. Never restart it. Report completion to the owner/Codex for final
backup and acceptance review.

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

If SSH fails, report `UNKNOWN: SSH unavailable`. Do not claim either service is
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
- The deployment marker is `b8e69d2`, unless
  `docs/PROJECT-STATUS.md` records a newer reviewed deployment.

Do not change a mismatched marker.

### 2. Service State

```bash
systemctl show \
  crypto-threshold-shadow.service \
  crypto-threshold-updown-shadow.service \
  --property=Id,ActiveState,SubState,Result,MainPID,NRestarts,ExecMainStatus,ExecMainStartTimestamp,InactiveEnterTimestamp \
  --no-pager
```

Interpretation:

- Daily before its scheduled completion: `active/running` is PASS.
- Daily after its scheduled completion: `inactive/dead`, `Result=success`, and
  `ExecMainStatus=0` is `EXPECTED_COMPLETE`, not a reason to restart.
- Up/Down: `active/running` is PASS.
- `failed`, non-zero exit, or any restart increase is FAIL.
- Current known restart baseline is zero for both services.

### 3. Recent Logs

```bash
sudo journalctl \
  -u crypto-threshold-shadow.service \
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
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only = ON")

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
                    SELECT last_status, next_attempt_at
                    FROM settlement_attempts
                    """,
                )
                now = datetime.now(UTC)
                due_count = sum(
                    1
                    for _, next_attempt_at in attempt_rows
                    if next_attempt_at and as_utc(next_attempt_at) <= now
                )
                print("settlement_attempt_statuses", fetchall(
                    connection,
                    """
                    SELECT last_status, COUNT(*)
                    FROM settlement_attempts
                    GROUP BY last_status
                    ORDER BY last_status
                    """,
                ))
                print("settlement_attempt_due_count", due_count)
                print("resolution_payload_summary", fetchall(
                    connection,
                    """
                    SELECT payload_kind, COUNT(*), MAX(id), MAX(received_at)
                    FROM external_payloads
                    WHERE payload_kind = 'chainlink_resolution_event'
                    GROUP BY payload_kind
                    """,
                ))
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
                SELECT COUNT(*)
                FROM analysis_signals AS s
                JOIN settlement_labels AS l
                  ON l.market_id = s.market_id
                WHERE ABS(
                    CAST(s.threshold AS REAL) - CAST(l.strike AS REAL)
                ) > 0.000000001
                """
            ).fetchone()[0]
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
            print("boundary_mismatch", boundary_mismatch)
            print("outcome_mismatch", outcome_mismatch)
    except Exception as error:
        print("ERROR", type(error).__name__, str(error))
    finally:
        if "connection" in locals():
            connection.close()
            del connection
PY
```

Freshness thresholds while a service is active:

| Database | PASS | WARN | FAIL |
|---|---:|---:|---:|
| Daily latest cycle | at most 10 minutes old | 10-20 minutes | over 20 minutes |
| Up/Down latest cycle | at most 5 minutes old | 5-10 minutes | over 10 minutes |

Additional interpretation:

- `forbidden_tables` must be `[]`. Match exact table names only.
  `analysis_signals` is valid and must not be rejected because it contains the
  substring `sign`.
- Up/Down `boundary_mismatch` and `outcome_mismatch` must both be zero.
- On schema v5, Up/Down must report `settlement_attempts` rather than
  `MISSING`. Pending attempts are acceptable when their retry time is in the
  future; a growing `settlement_attempt_due_count` is WARN and needs owner
  review.
- `resolution_payload_summary` and `top_resolution_payload_markets` are
  cursors for comparing checks. Live books and ticks may grow the overall
  payload table. A market's resolution payload count or max ID should not
  advance merely because Gamma returned the same settlement meaning. Existing
  historical duplicates are evidence, not a reason to delete rows.
- After warm-up, the latest 14 Up/Down rows normally contain seven 5m and seven
  15m signals. A single stale/rejected asset is WARN, not proof of a broken
  service.
- If Up/Down is WARN, wait five minutes and perform one read-only recheck. If it
  remains WARN or worsens, report it. Do not loop indefinitely.
- Rejections such as missing start tick, incomplete book, stale current tick,
  or insufficient volatility history are fail-closed research outcomes.
- Repeated cycles with all 14 signals rejected after warm-up are WARN and need
  Codex review, even when the service process remains active.

### 5. Backup Timers And Files

```bash
systemctl show \
  crypto-threshold-backup.timer \
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
  ls -lht /opt/polymarket-crypto-threshold/backups/updown
```

```bash
sudo -u crypto-threshold sh -c \
  'cd /opt/polymarket-crypto-threshold && \
   find backups -maxdepth 2 -type f -name "*.partial*" -print'
```

Expected:

- Both timers are `active/waiting` and `enabled`.
- The final `find` command prints nothing.
- A `.partial`, `.partial-wal`, or `.partial-shm` file is WARN. Report the exact
  filename and do not delete it.
- A missing scheduled backup or a backup older than 26 hours is WARN.

Do not manually start a backup service. Backup creation is a mutation and is
reserved for the owner/Codex.

## Verdict Rules

Use one overall verdict:

- `PASS`: SSH works, NTP is synchronized, expected services are healthy,
  evidence is fresh, no error logs, no forbidden tables, and backup timers are
  healthy.
- `WARN`: A transient rejection/degraded cycle, stale asset, delayed cycle,
  stale backup, partial file, deployment mismatch, or unexpected metric needs
  review but evidence is still readable.
- `FAIL`: A required service failed, active-service evidence is beyond the FAIL
  threshold, NTP is not synchronized, a DB is missing/unreadable, a forbidden
  table exists, boundary/outcome mismatch is non-zero, or logs show an
  unhandled exception.
- `UNKNOWN`: SSH or permissions prevented observation. Never convert UNKNOWN
  into PASS or FAIL by guessing.
- `EXPECTED_COMPLETE`: The bounded daily service ended successfully after its
  planned run. This still requires owner/Codex backup and acceptance review.

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
- Start/inactive time:
- DB schema:
- Cycle count and time range:
- Latest cycle age:
- Last 5 cycle statuses:
- Signal status counts:

Up/Down service
- ActiveState/SubState:
- Result/ExecMainStatus:
- MainPID:
- NRestarts:
- Start time:
- DB schema:
- Cycle count and time range:
- Latest cycle age:
- Last 5 cycle statuses:
- Latest 14 interval/status:
- Signal status counts:
- Paper status counts:
- Settlement label count:
- Settlement attempt statuses:
- Settlement attempt due count:
- Resolution payload summary/cursor:
- Boundary mismatch:
- Outcome mismatch:

Backups
- Daily timer:
- Up/Down timer:
- Latest backup timestamps:
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

- Daily service ends before its expected bound or exits non-zero
- Up/Down service is not active
- Either service restarts
- Three consecutive cycles are degraded or fail
- Evidence freshness crosses a FAIL threshold
- Both 5m and 15m remain fully rejected after warm-up and one recheck
- NTP changes from `yes`
- A forbidden table, mismatch, corruption, traceback, OOM, or disk-full message
  appears
- A timer is disabled or a scheduled backup is missing
- The bounded daily run completes successfully

When the daily run completes, remind the owner that old local history still
must not be deleted until the final VPS backup and evidence review are complete.
