"""Hand-written fakes: RDS, CloudWatch, a clock whose sleep moves time, a check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

REPLICA = "orders-dr"
ARN = f"arn:aws:rds:us-east-1:123456789012:db:{REPLICA}"


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeRDS:
    """After promote_read_replica, each describe walks through `states`."""

    def __init__(
        self,
        status="available",
        is_replica=True,
        env="staging",
        states=("modifying", "modifying", "available"),
        fail_on_describe_after=None,
        promote_error=None,
    ):
        self.status = status
        self.is_replica = is_replica
        self.env = env
        self.states = list(states)
        self.promoted = False
        self.promote_calls = 0
        self.describes_after = 0
        self.fail_on_describe_after = fail_on_describe_after
        self.promote_error = promote_error

    def describe_db_instances(self, DBInstanceIdentifier):
        assert DBInstanceIdentifier == REPLICA
        db = {
            "DBInstanceIdentifier": REPLICA,
            "DBInstanceArn": ARN,
            "DBInstanceStatus": self.status,
            "Endpoint": {"Address": f"{REPLICA}.example.com", "Port": 5432},
        }
        if self.promoted:
            self.describes_after += 1
            if self.describes_after == self.fail_on_describe_after:
                raise RuntimeError("Throttling")
            db["DBInstanceStatus"] = self.states.pop(0) if self.states else "available"
            # The source link goes away once promotion completes.
            if db["DBInstanceStatus"] == "available":
                return {"DBInstances": [db]}
            db["ReadReplicaSourceDBInstanceIdentifier"] = (
                "arn:aws:rds:eu-west-2:123456789012:db:orders"
            )
        elif self.is_replica:
            db["ReadReplicaSourceDBInstanceIdentifier"] = (
                "arn:aws:rds:eu-west-2:123456789012:db:orders"
            )
        return {"DBInstances": [db]}

    def list_tags_for_resource(self, ResourceName):
        assert ResourceName == ARN
        tags = [{"Key": "environment", "Value": self.env}] if self.env else []
        return {"TagList": tags}

    def promote_read_replica(self, DBInstanceIdentifier):
        self.promote_calls += 1
        self.promoted = True
        if self.promote_error:
            # AWS may have accepted the request even though the call errored.
            raise self.promote_error
        return {"DBInstance": {"DBInstanceIdentifier": DBInstanceIdentifier}}


class FakeCloudWatch:
    def __init__(self, lag_points=(2.0, 5.0)):
        self.lag_points = list(lag_points)
        self.calls = []

    def get_metric_statistics(self, **kw):
        self.calls.append(kw)
        return {"Datapoints": [{"Maximum": v} for v in self.lag_points]}


class FakeCheck:
    """Fails `failures` times, then succeeds. Advances nothing itself."""

    def __init__(self, failures=0, never=False):
        self.failures = failures
        self.never = never
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        if self.never or len(self.calls) <= self.failures:
            return False, f"TCP connect to {host}:{port} failed: refused"
        return True, f"TCP connect to {host}:{port} succeeded"
