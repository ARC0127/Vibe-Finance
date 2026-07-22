#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vibe_finance.evolution import (  # noqa: E402
    DEFAULT_LEDGER,
    DEFAULT_PORTFOLIO,
    sha256_file,
    verify_event_ledger,
    verify_portfolio_projection,
    write_json_atomic,
)


DEFAULT_GATE = ROOT / "reports/evolution/20260721-legacy-v0.3-remediation/gate.json"


def run(command: list[str]) -> dict:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def audit(gate_path: Path) -> dict:
    ledger = verify_event_ledger()
    replay = verify_portfolio_projection(ledger["_events"], DEFAULT_PORTFOLIO)
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    commands = {
        "shell_syntax": run(["bash", "-n", "scripts/sync_github.sh"]),
        "unit_tests": run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]),
        "diff_check": run(["git", "diff", "--check"]),
    }
    checks = {
        "ledger_git_head_prefix_and_chain": ledger["status"]
        == "PASS_COMMITTED_PREFIX_AND_CHAIN",
        "legacy_anchor": ledger["anchor"]["status"] == "VERIFIED_LEGACY_GIT_ANCHORED",
        "legacy_and_v2_partition_matches": ledger["event_count"]
        == ledger["legacy_event_count"] + ledger["v2_event_count"],
        "completed_round_trips_are_zero": replay["completed_round_trip_count"] == 0,
        "portfolio_projection": replay["portfolio_filled_trade_count"] == replay["filled_event_count"],
        "legacy_acceptance_invalidated": gate.get("legacy_acceptance_status")
        == "INVALID_LEGACY_ACCEPTANCE",
        "current_decision_is_proposed_only": gate.get("decision") == "PROPOSED_ONLY",
        "accepted_path_disabled": gate.get("claim_boundary", {}).get("accepted_path")
        == "DISABLED_UNTIL_TRUSTED_EVALUATOR_EXISTS",
        "all_verification_commands_pass": all(value["status"] == "PASS" for value in commands.values()),
    }
    sources = [
        "vibe_finance/evolution.py",
        "vibe_finance/pipeline.py",
        "vibe_finance/__main__.py",
        "scripts/sync_github.sh",
        "scripts/audit_evolution_gate.py",
        "config/ledger_legacy_anchor.json",
        "MODE_LOCK.json",
        "tests/test_evolution.py",
        "tests/test_sync_evolution.py",
        str(gate_path.relative_to(ROOT)),
        "data/ledger/orders.jsonl",
        "data/ledger/portfolio.json",
    ]
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS_STATIC_PROPOSED_ONLY_GATE" if all(checks.values()) else "FAIL",
        "checks": checks,
        "commands": commands,
        "ledger": {
            "event_count": ledger["event_count"],
            "legacy_event_count": ledger["legacy_event_count"],
            "v2_event_count": ledger["v2_event_count"],
            "git_head_prefix": ledger["git_head_prefix"],
            "head_event_sha256": ledger["head_event_sha256"],
            "completed_round_trip_count": replay["completed_round_trip_count"],
            "positions": replay["positions"],
        },
        "decision": {
            "current": gate["decision"],
            "legacy": gate["legacy_acceptance_status"],
            "reasons": gate["reasons"],
        },
        "claim_boundary": {
            "governance_mechanics": "STATIC_PROPOSED_ONLY_PATH_VERIFIED",
            "current_legacy_prefix": "GIT_HEAD_ANCHORED",
            "future_v2_append": "GIT_HEAD_PREFIX_REWRITE_REJECTED_IN_TEMP_REPO_TEST",
            "order_sample_fsm": "DIRECT_FILLED_FABRICATION_REJECTED",
            "pipeline_cross_file_transaction": "NOT_IMPLEMENTED",
            "sync_manifest_integration": "STATIC_AND_STAGED_CONTRACT_NOT_END_TO_END_RUN",
            "trusted_walk_forward_oos_evaluator": "NOT_IMPLEMENTED",
            "accepted_strategy_upgrade": "DISABLED",
            "strategy_promotion": "NOT_RUN",
            "rollback": "NOT_RUN",
            "github_sync": "NOT_RUN",
            "financial_data_modified_by_audit": False,
            "simulation_only": True,
        },
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "source_sha256": {path: sha256_file(ROOT / path) for path in sources},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.gate.resolve())
    if args.output:
        write_json_atomic(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "PASS_STATIC_PROPOSED_ONLY_GATE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
