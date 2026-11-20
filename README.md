# Cross-region RDS failover test

A Python script that tests disaster recovery for an RDS database by
promoting its cross-region read replica, waiting for it to become a
standalone primary, checking that the application can connect, timing the
whole thing, and writing a Markdown report. It runs as a **dry run** unless
you pass `--execute`.

> **Status:** AWS RDS only. **Azure SQL is not implemented.** 20 pytest
> tests drive the orchestration with fake RDS and CloudWatch clients, an
> injected clock and sleep, and a fake connectivity check. They cover
> pre-check refusals, timeouts, failed checks and API errors. Nothing has
> promoted a real database. The default TCP connectivity check and the
> boto3 wiring in `cli.py` are not exercised by tests.

## Problem

> Write a Python orchestration script that performs cross-region RDS /
> Azure SQL failover testing, validates application connectivity, and
> generates a report.

## Failover mechanism: cross-region read replica promotion

This uses `PromoteReadReplica` on a cross-region read replica. It works for
standard RDS engines (PostgreSQL, MySQL, MariaDB) with one API call, so it
was the simpler choice. Aurora Global Database has a managed switchover
that can fail back more easily, but it requires Aurora.

**Promotion is one-way.** The promoted instance stops replicating and
becomes an independent primary. Failing back means creating a new
cross-region replica and testing in the other direction. Run this against a
dedicated DR-test setup, not a replica you depend on.

## Flow

```mermaid
flowchart TD
    S[start] --> PC{pre-checks}
    PC -- status not available /<br/>not a replica /<br/>env=prod without --allow-prod /<br/>lag over threshold or no data --> X[PRECHECK_FAILED<br/>nothing changed, exit 2]
    PC -- ok --> DR{--execute?}
    DR -- no --> D[dry run report, exit 0]
    DR -- yes --> P[PromoteReadReplica<br/>start timer]
    P --> W{available and no longer<br/>a replica, before timeout?}
    W -- no --> F[FAIL report<br/>operator actions, exit 1]
    W -- yes --> C{connectivity check passes<br/>before timeout?}
    C -- no --> F
    C -- yes --> OK[PASS report<br/>time to recover, exit 0]
```

- **Pre-checks** run against the replica in the DR region: status
  `available`, it really is a read replica, the `environment` tag is present
  and is not `prod`/`production` (unless `--allow-prod`), and the maximum
  `ReplicaLag` over the last 5 minutes is under `--max-lag-seconds`. No lag
  data counts as a failure.
- **Polling** uses `DescribeDBInstances` every `--poll-seconds` until the
  instance is `available` with no replication source, or until
  `--timeout-seconds` since the promote call.
- **Connectivity** is checked through an injected function `check(host,
  port) -> (ok, detail)`. The default opens a TCP connection to the new
  endpoint. Swap in a real application health check (for example a
  `SELECT 1` with the app's credentials) for a stronger test.
- **Time to recover** runs from the promote call to the first successful
  connectivity check.

## Failure handling

A timeout, a failed connectivity check, or an API error at any point from the
promote call onwards produces a **FAIL** report and exit code 1. That
includes an error from `PromoteReadReplica` itself: the request may still
have reached AWS, so the outcome is ambiguous and is treated like any other
mid-test failure (report written, operator actions printed). The script **never rolls back on
its own**: a half-promoted database needs a human decision. The report and
stderr list what the operator must do (check the instance state, decide
which database the application should use, rebuild the replica if needed).

| Exit code | Meaning |
|---|---|
| 0 | dry run passed pre-checks, or the failover test passed |
| 1 | FAIL after promotion was triggered (see operator actions) |
| 2 | pre-checks failed; nothing was changed |

## Usage

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q

# Dry run (default): pre-checks only
python -m failover --replica-id orders-dr --region us-east-1

# Real test
python -m failover --replica-id orders-dr --region us-east-1 --execute \
  --max-lag-seconds 30 --timeout-seconds 1800 --report failover-report.md
```

`iam/policy.json` is a least-privilege policy: read-only describe, tags and
metrics, plus `rds:PromoteReadReplica` only on instances tagged
`environment=staging` or `dr-test` in account `123456789012` (placeholder).

## Limits

- AWS RDS only; Azure SQL is not implemented.
- The application is not repointed automatically; that depends on how it
  finds its database (DNS, config, secrets).
- One replica per run. Aurora Global Database is not supported.
- The default check proves the port is open, not that queries work.

## Security notes

- Dry run by default; promotion needs `--execute`, and production needs
  `--allow-prod` as well. The IAM policy adds a second guard by tag.
- No credentials in code; boto3 uses the standard credential chain.

## Idempotency

The test is deliberately not repeatable on the same replica: after a
successful promotion the instance is no longer a replica, so a second run
stops at the pre-checks with exit code 2 instead of acting again.
