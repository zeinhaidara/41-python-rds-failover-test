"""Markdown report for one failover test."""

from __future__ import annotations

from failover.orchestrator import Outcome


def to_markdown(out: Outcome) -> str:
    rto = f"{out.rto.total_seconds():.0f}s" if out.rto is not None else "n/a"
    lines = [
        f"# RDS failover test: {out.replica_id}",
        "",
        f"- Result: **{out.result}**",
        f"- Mode: {out.mode}",
        "- Mechanism: cross-region read replica promotion",
        f"- Time to recover (promote call to first successful connectivity check): {rto}",
        "",
        "## Steps",
        "",
        "| Time (UTC) | Status | Detail |",
        "|---|---|---|",
    ]
    for when, status, text in out.steps:
        lines.append(f"| {when.isoformat()} | {status} | {text} |")
    if out.operator_actions:
        lines += ["", "## Operator actions", ""]
        lines += [f"{i}. {a}" for i, a in enumerate(out.operator_actions, start=1)]
    return "\n".join(lines) + "\n"
