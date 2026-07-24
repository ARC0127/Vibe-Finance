from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.task_contracts import (
    TaskContractError,
    audit_task_contracts,
    load_task_contracts,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "task_contracts.json"
SYNC_SCRIPT = ROOT / "scripts" / "sync_github.sh"


class TaskContractTests(unittest.TestCase):
    def test_default_registry_matches_governed_sync_allowlists(self) -> None:
        result = audit_task_contracts(REGISTRY, SYNC_SCRIPT)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["claim_boundary"], "STRUCTURAL_GOVERNANCE_AUDIT_ONLY")
        self.assertTrue(result["simulation_only"])
        self.assertEqual(result["real_broker_integration"], "forbid")
        self.assertEqual(result["caller_force_bypass"], "forbid")
        self.assertEqual(result["sync_allowlist_status"], "PASS")
        self.assertEqual(result["task_count"], 11)

    def test_activity_monitor_has_no_financial_runtime_write_authority(self) -> None:
        result = audit_task_contracts(
            REGISTRY, SYNC_SCRIPT, task_id="activity-monitor"
        )
        contract = result["task"]
        self.assertEqual(contract["runtime_write_roots"], ["reports/monitor"])
        self.assertFalse(contract["allow_force_bypass"])
        self.assertTrue(
            any("must not refresh" in rule for rule in contract["constraints"])
        )

    def test_skill_memory_sync_excludes_unreviewed_candidates(self) -> None:
        result = audit_task_contracts(
            REGISTRY, SYNC_SCRIPT, task_id="skill-memory-review"
        )
        contract = result["task"]
        self.assertEqual(
            contract["runtime_write_roots"], ["reports/skill-memory/reviews"]
        )
        self.assertEqual(
            contract["sync_allowlist"], ["reports/skill-memory/reviews"]
        )
        self.assertNotIn(
            "artifacts/skill-memory/candidates",
            contract["sync_allowlist"],
        )
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("artifacts/skill-memory/candidates/", gitignore)

    def test_force_bypass_and_broker_integration_fail_closed(self) -> None:
        payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contracts.json"

            bypass = copy.deepcopy(payload)
            bypass["tasks"][0]["allow_force_bypass"] = True
            path.write_text(json.dumps(bypass), encoding="utf-8")
            with self.assertRaisesRegex(TaskContractError, "must be false"):
                load_task_contracts(path)

            broker = copy.deepcopy(payload)
            broker["real_broker_integration"] = "allow"
            path.write_text(json.dumps(broker), encoding="utf-8")
            with self.assertRaisesRegex(TaskContractError, "must be 'forbid'"):
                load_task_contracts(path)

    def test_repository_escape_and_sync_drift_are_rejected(self) -> None:
        payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "contracts.json"

            traversal = copy.deepcopy(payload)
            traversal["tasks"][0]["runtime_write_roots"] = ["../data/ledger"]
            path.write_text(json.dumps(traversal), encoding="utf-8")
            with self.assertRaisesRegex(TaskContractError, "repository-relative"):
                load_task_contracts(path)

            non_normalized = copy.deepcopy(payload)
            non_normalized["tasks"][0]["runtime_write_roots"] = [
                "reports//monitor"
            ]
            path.write_text(json.dumps(non_normalized), encoding="utf-8")
            with self.assertRaisesRegex(TaskContractError, "repository-relative"):
                load_task_contracts(path)

            path.write_text(json.dumps(payload), encoding="utf-8")
            script = root / "sync_github.sh"
            source = SYNC_SCRIPT.read_text(encoding="utf-8").replace(
                "allowlist=(reports/monitor README.md)",
                "allowlist=(reports/monitor)",
                1,
            )
            script.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(TaskContractError, "allowlist mismatch"):
                audit_task_contracts(path, script)

    def test_unknown_task_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(TaskContractError, "unknown task_id"):
            audit_task_contracts(REGISTRY, SYNC_SCRIPT, task_id="not-a-task")


if __name__ == "__main__":
    unittest.main()
