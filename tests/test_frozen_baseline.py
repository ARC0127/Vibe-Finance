from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.evaluation_execution import (
    canonical_sha256,
    plan_frozen_b0_orders,
    simulate_next_open,
)
from vibe_finance.frozen_baseline import (
    FrozenBaselineError,
    run_frozen_b0_decision,
    run_frozen_b0_mechanical,
    verify_frozen_b0_artifact,
    verify_frozen_b0_sources,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json"
SNAPSHOTS = [
    ROOT / "data" / "inbox" / "2026-07-19.json",
    ROOT / "data" / "inbox" / "2026-07-20.json",
]
PROTECTED_FINANCIAL_FILES = [
    ROOT / "data" / "ledger" / "portfolio.json",
    ROOT / "data" / "ledger" / "orders.jsonl",
    ROOT / "data" / "ledger" / "heartbeat.json",
]


class FrozenBaselineTests(unittest.TestCase):
    def _write_four_etf_snapshot(self, path: Path) -> None:
        def asset(
            symbol: str,
            close: float,
            history: list[float],
            daily_return: float,
            risk_bucket: str,
            exposure_group: str,
            asset_type: str = "equity_etf",
        ) -> dict:
            return {
                "symbol": symbol,
                "name": symbol,
                "asset_type": asset_type,
                "risk_bucket": risk_bucket,
                "exposure_group": exposure_group,
                "close": close,
                "daily_return": daily_return,
                "history": history[:-1] + [close],
                "history_adjusted_for_corporate_actions": True,
                "corporate_actions": [],
                "source_ids": ["source_a", "source_b", "eastmoney"],
                "trading_status": "TRADING",
                "lot_size": 100,
            }

        snapshot = {
            "schema_version": 1,
            "run_date": "2026-01-05",
            "as_of": "2026-01-05T15:00:00+08:00",
            "is_trading_day": True,
            "market_state": "closed",
            "daily_return_definition": "adjacent_total_return_close_simple_return",
            "history_definition": "total_return_close_rescaled_to_signal_raw_close",
            "indices": [
                {
                    "symbol": "000300",
                    "broad": True,
                    "daily_return": 0.0,
                    "source_ids": ["source_a", "source_b"],
                }
            ],
            "assets": [
                asset(
                    "510300",
                    9.9,
                    [8.0 + 0.1 * index for index in range(20)],
                    0.01,
                    "core_equity",
                    "csi300",
                ),
                asset(
                    "512100",
                    9.95,
                    [9.0 + 0.05 * index for index in range(20)],
                    -0.02,
                    "small_growth_equity",
                    "csi1000",
                ),
                asset(
                    "510880",
                    10.0,
                    [10.0] * 20,
                    0.0,
                    "dividend_equity",
                    "dividend",
                ),
                asset(
                    "518880",
                    10.0,
                    [10.0] * 20,
                    0.0,
                    "gold",
                    "gold",
                    "gold_etf",
                ),
            ],
            "evidence": [],
        }
        path.write_text(json.dumps(snapshot), encoding="utf-8")

    def test_repository_b0_sources_match_full_commit_and_hashes(self) -> None:
        result = verify_frozen_b0_sources(ROOT, MANIFEST)
        self.assertEqual(result["status"], "FROZEN_SOURCES_VERIFIED")
        self.assertEqual(
            result["universe_sha256"],
            "23a8197aa3e9166949f669070318422861d77e3ff3d8af1da12be3ebaf7d29b7",
        )
        self.assertEqual(
            result["sources_sha256"],
            "a82bbe176be533f889fcd2807626ce6d17078543515660cf023efbb00d45f073",
        )
        self.assertTrue(result["claim_boundary"]["source_identity_verified"])
        self.assertFalse(result["claim_boundary"]["golden_behavior_verified"])
        self.assertFalse(result["claim_boundary"]["strategy_returns_computed"])

    def test_manifest_cannot_relabel_pipeline_hash(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest["candidates"]["B0"]["pipeline_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(FrozenBaselineError, "pipeline source hash mismatch"):
                verify_frozen_b0_sources(ROOT, path)

    def test_historical_fixture_smoke_is_deterministic_and_financially_isolated(self) -> None:
        before = {path: path.read_bytes() for path in PROTECTED_FINANCIAL_FILES}
        first = run_frozen_b0_mechanical(ROOT, MANIFEST, SNAPSHOTS)
        second = run_frozen_b0_mechanical(ROOT, MANIFEST, SNAPSHOTS)

        self.assertEqual(first["status"], "PASS_FROZEN_B0_HISTORICAL_FIXTURE_SMOKE")
        self.assertEqual(first["behavior_sha256"], second["behavior_sha256"])
        self.assertEqual(
            first["replay_contract_sha256"], second["replay_contract_sha256"]
        )
        self.assertEqual(first["output_file_sha256"], second["output_file_sha256"])
        self.assertEqual(
            [run["new_orders"] for run in first["behavior"]["runs"]], [0, 2]
        )
        orders = first["behavior"]["decisions"][1]["new_orders"]
        self.assertEqual(
            [(order["symbol"], order["side"], order["quantity"]) for order in orders],
            [("510300", "BUY", 1200), ("512100", "BUY", 800)],
        )
        self.assertTrue(
            all(first["protected_financial_files_unchanged"].values())
        )
        self.assertEqual(
            first["determinism"]["network_guard"],
            "PYTHON_SOCKET_CONNECT_DENIED_APPLICATION_LEVEL",
        )
        self.assertTrue(first["claim_boundary"]["historical_fixture_only"])
        self.assertFalse(first["claim_boundary"]["fair_evaluation_adapter_ready"])
        self.assertFalse(first["claim_boundary"]["strategy_returns_computed"])
        self.assertFalse(first["claim_boundary"]["promotion_authorized"])
        for path, expected in before.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_saved_smoke_artifact_reloads_replays_and_rejects_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "frozen-b0-smoke.json"
            saved = run_frozen_b0_mechanical(
                ROOT, MANIFEST, SNAPSHOTS, output_path=artifact_path
            )
            with self.assertRaisesRegex(FrozenBaselineError, "already exists"):
                run_frozen_b0_mechanical(
                    ROOT, MANIFEST, SNAPSHOTS, output_path=artifact_path
                )
            verified = verify_frozen_b0_artifact(ROOT, MANIFEST, artifact_path)
            self.assertEqual(
                verified["status"],
                "VERIFIED_FROZEN_B0_HISTORICAL_FIXTURE_SMOKE",
            )
            self.assertEqual(
                verified["replayed_behavior_sha256"], saved["behavior_sha256"]
            )
            self.assertFalse(verified["claim_boundary"]["strategy_returns_verified"])

            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["behavior"]["runs"][1]["new_orders"] = 99
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            invalid = verify_frozen_b0_artifact(ROOT, MANIFEST, artifact_path)
            self.assertEqual(invalid["status"], "INVALID")
            self.assertIn("ARTIFACT_CONTRACT_HASH_MISMATCH", invalid["mismatches"])

    def test_artifact_verification_fails_closed_when_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            copied_inputs = []
            for source in SNAPSHOTS:
                target = temporary / source.name
                target.write_bytes(source.read_bytes())
                copied_inputs.append(target)
            artifact_path = temporary / "artifact.json"
            run_frozen_b0_mechanical(
                ROOT, MANIFEST, copied_inputs, output_path=artifact_path
            )
            payload = json.loads(copied_inputs[1].read_text(encoding="utf-8"))
            payload["adapter_tamper_test"] = True
            copied_inputs[1].write_text(json.dumps(payload), encoding="utf-8")
            invalid = verify_frozen_b0_artifact(ROOT, MANIFEST, artifact_path)
            self.assertEqual(invalid["status"], "INVALID")
            self.assertTrue(
                any(
                    mismatch.startswith("INPUT_HASH_MISMATCH:")
                    for mismatch in invalid["mismatches"]
                )
            )

    def test_frozen_decision_adapter_skips_historical_settlement_and_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            self._write_four_etf_snapshot(snapshot)
            calendar = ["2026-01-05", "2026-01-06"]
            source_registry = {
                "sources": [
                    {
                        "id": "source_a",
                        "independence_group": "group_a",
                        "allowed_uses": ["local_research_evaluation"],
                    },
                    {
                        "id": "source_b",
                        "independence_group": "group_b",
                        "allowed_uses": ["local_research_evaluation"],
                    },
                ]
            }
            bindings = {
                "readiness_artifact_sha256": "a" * 64,
                "canonical_panel_sha256": "b" * 64,
                "source_registry_sha256": canonical_sha256(source_registry),
                "calendar_sha256": canonical_sha256(calendar),
            }
            first = run_frozen_b0_decision(
                ROOT,
                MANIFEST,
                snapshot,
                {"cash_cny": 29900.0, "positions": {}},
                trading_calendar_dates=calendar,
                data_bindings=bindings,
            )
            second = run_frozen_b0_decision(
                ROOT,
                MANIFEST,
                snapshot,
                {"cash_cny": 29900.0, "positions": {}},
                trading_calendar_dates=calendar,
                data_bindings=bindings,
            )
            self.assertEqual(first, second)
            self.assertEqual(
                [(order["symbol"], order["quantity"]) for order in first["orders"]],
                [("510300", 600), ("512100", 200)],
            )
            self.assertEqual(first["orders"][0]["raw_open_limit"], 10.0485)
            self.assertEqual(first["orders"][1]["raw_open_limit"], 10.09925)
            self.assertNotIn("order_id", first["orders"][0])
            self.assertTrue(
                first["claim_boundary"]["historical_settlement_skipped"]
            )
            self.assertTrue(first["claim_boundary"]["historical_heartbeat_skipped"])
            self.assertFalse(first["claim_boundary"]["strategy_returns_computed"])

            plan = plan_frozen_b0_orders(
                frozen_signal=first,
                execute_date="2026-01-06",
                trading_calendar_dates=calendar,
                data_bindings=bindings,
                current_cash_cny=29900.0,
                current_quantities={},
                available_to_sell_quantities={},
                signal_closes={
                    "510300": 9.9,
                    "510880": 10.0,
                    "512100": 9.95,
                    "518880": 10.0,
                },
                nav_cny=29900.0,
            )
            open_observations = {
                symbol: {
                    "trading_status": "TRADING",
                    "corporate_actions": [],
                    "observations": [
                        {
                            "value": 10.0,
                            "observed_at": "2026-01-06T09:31:00+08:00",
                            "source_id": source_id,
                            "raw_content_sha256": character * 64,
                        }
                        for source_id, character in (
                            ("source_a", "a"),
                            ("source_b", "b"),
                        )
                    ],
                }
                for symbol in ("510300", "512100")
            }
            execution = simulate_next_open(
                plan,
                open_observations=open_observations,
                source_registry=source_registry,
                execution_cutoff="2026-01-06T09:35:00+08:00",
                cash_cny=29900.0,
                current_quantities={},
                available_to_sell_quantities={},
            )
            self.assertEqual(execution["status"], "FILLED")
            self.assertEqual(execution["after_state"]["cash_cny"], 21882.0)
            self.assertEqual(execution["commission_cny"], 10.0)
            self.assertEqual(execution["slippage_cny"], 8.0)
            self.assertEqual(
                execution["after_state"]["position_accounting"]["510300"],
                {
                    "quantity": 600,
                    "average_cost": 10.018333,
                    "acquired_date": "2026-01-06",
                    "last_buy_date": "2026-01-06",
                },
            )


if __name__ == "__main__":
    unittest.main()
