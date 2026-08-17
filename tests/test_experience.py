from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.experience import (
    ExperienceLedgerError,
    load_preopen_experience_context,
    sha256_file,
    validate_experience_ledger,
    write_experience_record,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ExperienceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.experience_root = self.root / "reports/evolution"
        self.evidence_path = self.root / "reports/weekly/2026-W33.md"
        self.evidence_path.parent.mkdir(parents=True)
        self.evidence_path.write_text("sealed weekly evidence\n", encoding="utf-8")
        self.strategy_path = self.root / "config/strategy.json"
        write_json(self.strategy_path, {"version": "test-v1"})
        self.mode_lock_path = self.root / "MODE_LOCK.json"
        write_json(
            self.mode_lock_path,
            {
                "evolution_policy": {
                    "minimum_completed_virtual_trades_for_parameter_upgrade": 20,
                    "walk_forward_and_independent_oos_required": True,
                    "trusted_evaluator_required": True,
                },
                "experience_ledger_policy": {
                    "preopen_reader": "required_fail_closed",
                    "unpromoted_strategy_effect": "forbid",
                    "promotion_authority": "protected_static_verifier_only",
                    "active_rule_requires_live_strategy_hash_match": True,
                },
            },
        )
        verifier = self.root / "vibe_finance/evolution.py"
        verifier.parent.mkdir(parents=True)
        verifier.write_text("# protected verifier fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def draft(
        self,
        *,
        experience_id: str = "EXP-W33-FEE-FRICTION",
        revision: int = 1,
        previous: str | None = None,
        validation_status: str = "OBSERVED",
        eligible: int = 0,
        impact_status: str = "NONE",
        run_id: str = "test-run",
    ) -> dict:
        impact = {
            "status": impact_status,
            "target": "execution.minimum_expected_edge",
            "proposed_change": "Require expected edge to cover explicit costs.",
            "rollback_condition": "Rollback if closed-loop net return degrades.",
        }
        if impact_status == "PROPOSED_ONLY":
            impact.update(
                {
                    "evolution_run_id": run_id,
                    "candidate_strategy_sha256": sha256_file(self.strategy_path),
                }
            )
        return {
            "schema_version": 1,
            "experience_id": experience_id,
            "revision": revision,
            "previous_record_sha256": previous,
            "recorded_at": "2026-08-17T10:00:00+08:00",
            "as_of": "2026-08-14T16:30:00+08:00",
            "created_by_task": "reflection-evolution",
            "observation": {
                "statement": "Minimum fees materially affected the weekly result."
            },
            "hypothesis": {
                "statement": "A cost-aware entry threshold can improve net results.",
                "falsification_criteria": "Reject if eligible closed trades do not improve net return.",
            },
            "evidence": [
                {
                    "path": "reports/weekly/2026-W33.md",
                    "sha256": sha256_file(self.evidence_path),
                    "claim": "The report separates market PnL and fees.",
                }
            ],
            "counterevidence": [
                {
                    "path": "reports/weekly/2026-W33.md",
                    "sha256": sha256_file(self.evidence_path),
                    "claim": "A one-week fee share does not establish a stable threshold.",
                }
            ],
            "counterevidence_search": {
                "status": "FOUND",
                "notes": "The same report limits the inference to one week.",
            },
            "scope": {
                "market_scope": "CN_MAINLAND_PUBLIC_MARKETS",
                "asset_types": ["equity_etf", "gold_etf"],
                "horizon": "next 20 eligible closed round trips",
                "regimes": ["normal liquidity"],
                "exclusions": ["open-end funds", "real trading"],
            },
            "validation": {
                "status": validation_status,
                "method": "Compare cost-aware and baseline decisions on sealed events.",
                "completed_round_trips": eligible,
                "eligible_round_trips": eligible,
                "metrics": {"weekly_fee_cny": 15.0},
                "limitations": ["Single-week observation"],
            },
            "strategy_impact": impact,
        }

    def record(self, draft: dict, run_id: str = "test-run") -> tuple[Path, dict]:
        draft_path = self.root / f"{run_id}-draft.json"
        write_json(draft_path, draft)
        output = self.experience_root / run_id / "experience.json"
        result = write_experience_record(
            draft_path,
            output,
            experience_root=self.experience_root,
            repo_root=self.root,
        )
        return output, result

    def test_record_reload_and_preopen_context(self) -> None:
        output, result = self.record(self.draft())
        self.assertEqual(result["status"], "RECORDED")
        self.assertTrue(output.exists())

        ledger = validate_experience_ledger(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(ledger["status"], "PASS")
        self.assertEqual(ledger["record_count"], 1)
        self.assertEqual(ledger["experience_count"], 1)
        self.assertEqual(ledger["promoted_rule_count"], 0)

        context = load_preopen_experience_context(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(context["experience_count"], 1)
        self.assertEqual(context["review_items"][0]["strategy_status"], "NON_BINDING")

    def test_revision_chain_is_hash_bound(self) -> None:
        _, first = self.record(self.draft(), run_id="run-one")
        second = self.draft(
            revision=2,
            previous=first["record_sha256"],
            validation_status="HYPOTHESIS",
        )
        self.record(second, run_id="run-two")
        ledger = validate_experience_ledger(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(ledger["record_count"], 2)
        self.assertEqual(ledger["experiences"][0]["revision"], 2)

        bad = self.draft(revision=3, previous="0" * 64)
        with self.assertRaisesRegex(ExperienceLedgerError, "previous_record_sha256"):
            self.record(bad, run_id="run-three")

    def test_evidence_drift_fails_closed(self) -> None:
        self.record(self.draft())
        self.evidence_path.write_text("drifted evidence\n", encoding="utf-8")
        with self.assertRaisesRegex(ExperienceLedgerError, "SHA-256 mismatch"):
            validate_experience_ledger(
                self.experience_root,
                repo_root=self.root,
                mode_lock_path=self.mode_lock_path,
                strategy_path=self.strategy_path,
            )

    def test_supported_experience_requires_independent_accepted_gate(self) -> None:
        run_id = "accepted-run"
        output, result = self.record(
            self.draft(
                validation_status="SUPPORTED",
                eligible=20,
                impact_status="PROPOSED_ONLY",
                run_id=run_id,
            ),
            run_id=run_id,
        )
        before = validate_experience_ledger(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(before["experiences"][0]["strategy_status"], "PROPOSED_ONLY")
        self.assertIn(
            "INDEPENDENT_GATE_MISSING",
            before["experiences"][0]["strategy_reasons"],
        )

        gate = {
            "decision": "ACCEPTED",
            "run_id": run_id,
            "ledger": {"eligible_sample_count": 20},
            "claim_boundary": {
                "walk_forward": "VERIFIED",
                "independent_oos": "VERIFIED",
                "rollback_replay": "VERIFIED",
                "accepted_path": "ENABLED_BY_TRUSTED_EVALUATOR",
            },
            "candidate": {"sha256": sha256_file(self.strategy_path)},
            "evidence": {"experience": {"sha256": sha256_file(output)}},
            "verifier": {
                "source_sha256": sha256_file(self.root / "vibe_finance/evolution.py")
            },
            "source_sha256": {"mode_lock": sha256_file(self.mode_lock_path)},
        }
        write_json(output.parent / "gate.json", gate)
        after = validate_experience_ledger(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(after["promoted_rule_count"], 1)
        self.assertEqual(after["active_strategy_rules"][0]["record_sha256"], result["record_sha256"])

    def test_forged_gate_verifier_does_not_promote(self) -> None:
        run_id = "forged-run"
        output, _ = self.record(
            self.draft(
                validation_status="SUPPORTED",
                eligible=20,
                impact_status="PROPOSED_ONLY",
                run_id=run_id,
            ),
            run_id=run_id,
        )
        gate = {
            "decision": "ACCEPTED",
            "run_id": run_id,
            "ledger": {"eligible_sample_count": 20},
            "claim_boundary": {
                "walk_forward": "VERIFIED",
                "independent_oos": "VERIFIED",
                "rollback_replay": "VERIFIED",
                "accepted_path": "ENABLED_BY_TRUSTED_EVALUATOR",
            },
            "candidate": {"sha256": sha256_file(self.strategy_path)},
            "evidence": {"experience": {"sha256": sha256_file(output)}},
            "verifier": {"source_sha256": hashlib.sha256(b"forged").hexdigest()},
            "source_sha256": {"mode_lock": sha256_file(self.mode_lock_path)},
        }
        write_json(output.parent / "gate.json", gate)
        ledger = validate_experience_ledger(
            self.experience_root,
            repo_root=self.root,
            mode_lock_path=self.mode_lock_path,
            strategy_path=self.strategy_path,
        )
        self.assertEqual(ledger["promoted_rule_count"], 0)
        self.assertIn("UNTRUSTED_GATE_VERIFIER", ledger["experiences"][0]["strategy_reasons"])


if __name__ == "__main__":
    unittest.main()
