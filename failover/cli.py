"""python -m failover --replica-id orders-dr --region us-east-1 [--execute]"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from failover.orchestrator import Options, Orchestrator, tcp_check
from failover.report import to_markdown

EXIT = {"PASS": 0, "FAIL": 1, "PRECHECK_FAILED": 2}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cross-region RDS replica promotion test.")
    p.add_argument("--replica-id", required=True, help="cross-region read replica to promote")
    p.add_argument("--region", help="region of the replica (the DR region)")
    p.add_argument("--max-lag-seconds", type=float, default=30)
    p.add_argument("--timeout-seconds", type=float, default=1800)
    p.add_argument("--poll-seconds", type=float, default=30)
    p.add_argument("--allow-prod", action="store_true", help="allow environment=prod")
    p.add_argument("--execute", action="store_true", help="really promote (default: dry-run)")
    p.add_argument("--report", type=Path, default=Path("failover-report.md"))
    return p.parse_args(argv)


def main(argv=None, rds=None, cloudwatch=None, check=tcp_check, clock=None, sleep=None) -> int:
    args = parse_args(argv)
    clock = clock or (lambda: datetime.now(UTC))
    sleep = sleep or time.sleep
    try:
        if rds is None or cloudwatch is None:
            import boto3

            session = boto3.Session(region_name=args.region)
            rds = rds or session.client("rds")
            cloudwatch = cloudwatch or session.client("cloudwatch")
        opts = Options(
            args.replica_id,
            args.max_lag_seconds,
            args.timeout_seconds,
            args.poll_seconds,
            args.allow_prod,
            args.execute,
        )
        outcome = Orchestrator(rds, cloudwatch, opts, check, clock, sleep).run()
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "If PromoteReadReplica was already called, check the instance state in the "
            "RDS console before doing anything else.",
            file=sys.stderr,
        )
        return 1

    args.report.write_text(to_markdown(outcome), encoding="utf-8")
    print(f"{outcome.result} ({outcome.mode}); report written to {args.report}")
    for action in outcome.operator_actions:
        print(f"operator: {action}", file=sys.stderr if outcome.result != "PASS" else sys.stdout)
    return EXIT[outcome.result]
