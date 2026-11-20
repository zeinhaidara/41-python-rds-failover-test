"""Cross-region read replica promotion test: pre-check, promote, poll, verify, time it."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

PROTECTED_ENVS = {"prod", "production"}

Check = Callable[[str, int], tuple[bool, str]]


def tcp_check(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Default connectivity check: can we open a TCP connection to the endpoint?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP connect to {host}:{port} succeeded"
    except OSError as exc:
        return False, f"TCP connect to {host}:{port} failed: {exc}"


@dataclass
class Options:
    replica_id: str
    max_lag_seconds: float = 30
    timeout_seconds: float = 1800
    poll_seconds: float = 30
    allow_prod: bool = False
    execute: bool = False


@dataclass
class Outcome:
    replica_id: str
    mode: str  # "dry-run" or "execute"
    result: str = "PASS"  # PASS, FAIL, PRECHECK_FAILED
    steps: list[tuple[datetime, str, str]] = field(default_factory=list)  # (when, status, text)
    triggered_at: datetime | None = None
    recovered_at: datetime | None = None
    operator_actions: list[str] = field(default_factory=list)

    def log(self, when: datetime, status: str, text: str) -> None:
        self.steps.append((when, status, text))

    @property
    def rto(self) -> timedelta | None:
        if self.triggered_at and self.recovered_at:
            return self.recovered_at - self.triggered_at
        return None


class Orchestrator:
    def __init__(
        self,
        rds,
        cloudwatch,
        opts: Options,
        check: Check = tcp_check,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.rds, self.cw, self.opts, self.check = rds, cloudwatch, opts, check
        if clock is None or sleep is None:
            raise ValueError("clock and sleep must be injected")
        self.clock, self.sleep = clock, sleep

    def _describe(self) -> dict:
        resp = self.rds.describe_db_instances(DBInstanceIdentifier=self.opts.replica_id)
        return resp["DBInstances"][0]

    # --- pre-checks --------------------------------------------------------------

    def precheck(self, out: Outcome) -> dict | None:
        db = self._describe()
        problems = []
        if db["DBInstanceStatus"] != "available":
            problems.append(f"status is {db['DBInstanceStatus']}, expected available")
        if not db.get("ReadReplicaSourceDBInstanceIdentifier"):
            problems.append("instance is not a read replica")

        tags = {
            t["Key"]: t["Value"]
            for t in self.rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"]
        }
        env = tags.get("environment", "").lower()
        if not env:
            problems.append("no environment tag; refusing to guess")
        elif env in PROTECTED_ENVS and not self.opts.allow_prod:
            problems.append(f"environment is {env}; pass --allow-prod to test it")

        lag = self._max_lag(db["DBInstanceIdentifier"])
        if lag is None:
            problems.append("no ReplicaLag data in the last 5 minutes")
        elif lag > self.opts.max_lag_seconds:
            problems.append(f"replica lag {lag:.0f}s is over {self.opts.max_lag_seconds:.0f}s")

        now = self.clock()
        if problems:
            for p in problems:
                out.log(now, "FAIL", f"pre-check: {p}")
            out.result = "PRECHECK_FAILED"
            out.operator_actions.append("Nothing was changed. Fix the pre-check failures above.")
            return None
        out.log(
            now,
            "OK",
            f"pre-checks passed (env={env}, lag={lag:.0f}s, "
            f"source={db['ReadReplicaSourceDBInstanceIdentifier']})",
        )
        return db

    def _max_lag(self, instance_id: str) -> float | None:
        end = self.clock()
        resp = self.cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="ReplicaLag",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
            StartTime=end - timedelta(minutes=5),
            EndTime=end,
            Period=60,
            Statistics=["Maximum"],
        )
        points = resp.get("Datapoints", [])
        return max(p["Maximum"] for p in points) if points else None

    # --- the test -----------------------------------------------------------------

    def run(self) -> Outcome:
        out = Outcome(self.opts.replica_id, "execute" if self.opts.execute else "dry-run")
        if self.precheck(out) is None:
            return out
        if not self.opts.execute:
            out.log(self.clock(), "SKIP", "dry-run: would call PromoteReadReplica; pass --execute")
            return out

        out.triggered_at = self._started = self.clock()
        self.rds.promote_read_replica(DBInstanceIdentifier=self.opts.replica_id)
        out.log(out.triggered_at, "OK", "PromoteReadReplica called")
        try:
            return self._after_trigger(out)
        except Exception as exc:
            # Something broke mid-test: record it as a FAIL so the report still gets written.
            return self._fail(out, f"error after promotion: {type(exc).__name__}: {exc}")

    def _after_trigger(self, out: Outcome) -> Outcome:
        db = self._wait_promoted(out)
        if db is None:
            return self._fail(out, "promotion did not finish within the timeout")
        endpoint = db["Endpoint"]
        ok, detail = self._wait_connectivity(out, endpoint["Address"], endpoint["Port"])
        if not ok:
            return self._fail(out, f"connectivity check never passed: {detail}")
        out.recovered_at = self.clock()
        out.log(out.recovered_at, "OK", detail)
        out.operator_actions += [
            "The replica is now a standalone primary. Point the application at it if it "
            "isn't already.",
            "Promotion is one-way: to fail back, create a new cross-region replica and "
            "repeat the test in the other direction.",
        ]
        return out

    def _deadline_passed(self) -> bool:
        return self.clock() - self._started >= timedelta(seconds=self.opts.timeout_seconds)

    def _wait_promoted(self, out: Outcome) -> dict | None:
        while True:
            db = self._describe()
            promoted = not db.get("ReadReplicaSourceDBInstanceIdentifier")
            if promoted and db["DBInstanceStatus"] == "available":
                out.log(self.clock(), "OK", "instance is available and no longer a replica")
                return db
            if self._deadline_passed():
                out.log(self.clock(), "FAIL", f"still {db['DBInstanceStatus']} at timeout")
                return None
            self.sleep(self.opts.poll_seconds)

    def _wait_connectivity(self, out: Outcome, host: str, port: int) -> tuple[bool, str]:
        while True:
            ok, detail = self.check(host, port)
            if ok:
                return True, detail
            out.log(self.clock(), "WAIT", detail)
            if self._deadline_passed():
                return False, detail
            self.sleep(self.opts.poll_seconds)

    def _fail(self, out: Outcome, why: str) -> Outcome:
        out.result = "FAIL"
        out.log(self.clock(), "FAIL", why)
        # Never roll back on our own: a half-promoted database needs a human decision.
        out.operator_actions += [
            f"Check {self.opts.replica_id} in the RDS console: it may be partly or fully "
            "promoted. Do not re-run this test until you know its state.",
            "Decide whether the application should use the promoted instance or the "
            "original primary, and update connection settings accordingly.",
            "If the old primary is still the source of truth, delete or rename the promoted "
            "instance and create a fresh cross-region replica.",
        ]
        return out
