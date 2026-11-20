from datetime import timedelta

import pytest

from failover.cli import main
from failover.orchestrator import Options, Orchestrator
from failover.report import to_markdown
from tests.fakes import REPLICA, FakeCheck, FakeClock, FakeCloudWatch, FakeRDS


def orchestrate(rds=None, cw=None, check=None, clock=None, **opts):
    clock = clock or FakeClock()
    o = Orchestrator(
        rds or FakeRDS(),
        cw or FakeCloudWatch(),
        Options(REPLICA, **opts),
        check or FakeCheck(),
        clock,
        clock.sleep,
    )
    return o.run(), clock


# --- dry run and pre-checks --------------------------------------------------------


def test_dry_run_is_the_default_and_promotes_nothing():
    rds = FakeRDS()
    out, _ = orchestrate(rds=rds)
    assert out.result == "PASS" and out.mode == "dry-run"
    assert rds.promote_calls == 0
    assert "would call PromoteReadReplica" in out.steps[-1][2]


@pytest.mark.parametrize(
    ("rds", "cw", "message"),
    [
        (FakeRDS(status="backing-up"), FakeCloudWatch(), "status is backing-up"),
        (FakeRDS(is_replica=False), FakeCloudWatch(), "not a read replica"),
        (FakeRDS(env="prod"), FakeCloudWatch(), "--allow-prod"),
        (FakeRDS(env="Production"), FakeCloudWatch(), "--allow-prod"),
        (FakeRDS(env=None), FakeCloudWatch(), "no environment tag"),
        (FakeRDS(), FakeCloudWatch(lag_points=[3.0, 45.0]), "replica lag 45s is over 30s"),
        (FakeRDS(), FakeCloudWatch(lag_points=[]), "no ReplicaLag data"),
    ],
)
def test_precheck_failures_change_nothing(rds, cw, message):
    out, _ = orchestrate(rds=rds, cw=cw, execute=True)
    assert out.result == "PRECHECK_FAILED"
    assert rds.promote_calls == 0
    assert any(message in text for _, _, text in out.steps)


def test_prod_allowed_with_flag():
    rds = FakeRDS(env="prod")
    out, _ = orchestrate(rds=rds, allow_prod=True)
    assert out.result == "PASS"


def test_lag_query_shape():
    cw = FakeCloudWatch()
    orchestrate(cw=cw)
    call = cw.calls[0]
    assert call["MetricName"] == "ReplicaLag"
    assert call["Dimensions"] == [{"Name": "DBInstanceIdentifier", "Value": REPLICA}]
    assert call["EndTime"] - call["StartTime"] == timedelta(minutes=5)


# --- execute ------------------------------------------------------------------------


def test_successful_failover_measures_time_to_recover():
    rds, check = FakeRDS(), FakeCheck(failures=2)
    out, clock = orchestrate(rds=rds, check=check, execute=True, poll_seconds=30)
    assert out.result == "PASS"
    assert rds.promote_calls == 1
    # 2 polls while modifying, then 2 failed connectivity checks: 4 x 30s.
    assert out.rto == timedelta(seconds=120)
    assert check.calls[0] == (f"{REPLICA}.example.com", 5432)
    assert any("one-way" in a for a in out.operator_actions)


def test_promotion_timeout_is_a_fail_with_operator_actions():
    rds = FakeRDS(states=["modifying"] * 100)
    out, _ = orchestrate(rds=rds, execute=True, timeout_seconds=300, poll_seconds=60)
    assert out.result == "FAIL"
    assert out.rto is None
    assert any("may be partly or fully promoted" in a for a in out.operator_actions)
    assert any("within the timeout" in text for _, _, text in out.steps)


def test_connectivity_never_passing_is_a_fail():
    out, _ = orchestrate(
        check=FakeCheck(never=True), execute=True, timeout_seconds=300, poll_seconds=60
    )
    assert out.result == "FAIL"
    assert any("connectivity check never passed" in text for _, _, text in out.steps)


def test_api_error_after_promotion_still_reports_fail():
    rds = FakeRDS(fail_on_describe_after=2)
    out, _ = orchestrate(rds=rds, execute=True)
    assert out.result == "FAIL"
    assert any("Throttling" in text for _, _, text in out.steps)
    assert rds.promote_calls == 1  # and nothing tried to undo it


def test_clock_and_sleep_must_be_injected():
    with pytest.raises(ValueError):
        Orchestrator(FakeRDS(), FakeCloudWatch(), Options(REPLICA), FakeCheck())


# --- report and CLI ---------------------------------------------------------------------


def test_report_contents():
    out, _ = orchestrate(check=FakeCheck(failures=1), execute=True)
    md = to_markdown(out)
    assert "- Result: **PASS**" in md
    assert "- Mechanism: cross-region read replica promotion" in md
    assert "Time to recover (promote call to first successful connectivity check): 90s" in md
    assert "| OK | PromoteReadReplica called |" in md


def run_cli(tmp_path, rds, *extra, check=None, cw=None):
    clock = FakeClock()
    report = tmp_path / "r.md"
    code = main(
        ["--replica-id", REPLICA, "--report", str(report), *extra],
        rds=rds,
        cloudwatch=cw or FakeCloudWatch(),
        check=check or FakeCheck(),
        clock=clock,
        sleep=clock.sleep,
    )
    return code, report


def test_cli_exit_codes(tmp_path):
    assert run_cli(tmp_path, FakeRDS())[0] == 0  # dry-run
    assert run_cli(tmp_path, FakeRDS(env="prod"), "--execute")[0] == 2
    code, report = run_cli(
        tmp_path, FakeRDS(), "--execute", "--timeout-seconds", "60", check=FakeCheck(never=True)
    )
    assert code == 1
    assert "**FAIL**" in report.read_text(encoding="utf-8")


def test_cli_prints_operator_actions_on_fail(tmp_path, capsys):
    run_cli(tmp_path, FakeRDS(states=["modifying"] * 100), "--execute", "--timeout-seconds", "60")
    assert "operator: Check orders-dr" in capsys.readouterr().err
