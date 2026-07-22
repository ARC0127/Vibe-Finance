from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.evaluation import (
    EvaluationDataError,
    audit_evaluation_readiness,
    build_walk_forward_splits,
    verify_readiness_artifact,
    write_readiness_artifact,
)


ROOT = Path(__file__).resolve().parents[1]


class EvaluationReadinessTests(unittest.TestCase):
    def test_current_repository_data_is_explicitly_not_evaluable(self) -> None:
        result = audit_evaluation_readiness(
            list((ROOT / "data" / "inbox").glob("*.json")),
            sources_path=ROOT / "config" / "sources.json",
            universe_path=ROOT / "config" / "universe.json",
            manifest_path=ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json",
            calendar_path=ROOT / "config" / "evaluation" / "trading-calendar.json",
        )
        self.assertEqual(result["status"], "NOT_EVALUABLE")
        self.assertFalse(result["claim_boundary"]["strategy_returns_computed"])
        self.assertIn("MINIMUM_COMMON_UNIQUE_DATES_0_LT_2016", result["reasons"])
        self.assertTrue(
            any(reason.startswith("SNAPSHOT_SCHEMA_NOT_V2") for reason in result["reasons"])
        )
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "readiness.json"
            write_readiness_artifact(artifact, result)
            verified = verify_readiness_artifact(artifact)
            self.assertEqual(verified["status"], "VERIFIED_NOT_EVALUABLE")

            tampered = json.loads(artifact.read_text(encoding="utf-8"))
            tampered["status"] = "READY_FOR_MECHANICAL_EVALUATION"
            artifact.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(verify_readiness_artifact(artifact)["status"], "INVALID")
        self.assertTrue(
            any(
                reason.startswith("SOURCE_LICENSE_NOT_VERIFIED")
                for reason in result["reasons"]
            )
        )

    def test_manifest_bound_complete_fixture_can_pass_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "dataset_contract": {
                            "minimum_common_history_trading_days": 4,
                            "point_in_time_fields": [
                                "date",
                                "symbol",
                                "raw_open",
                                "raw_close",
                                "total_return_close",
                                "open_observed_at",
                                "close_observed_at",
                                "open_source_ids",
                                "close_source_ids",
                                "trading_status",
                                "corporate_actions",
                                "license_status",
                                "snapshot_sha256",
                            ],
                        },
                        "splits": {
                            "maximum_forward_label_horizon_trading_days": 0,
                            "walk_forward": {
                                "development_window_trading_days": 1,
                                "validation_window_trading_days": 1,
                                "test_window_trading_days": 1,
                                "step_trading_days": 1,
                            },
                            "independent_oos": {"sealed_tail_trading_days": 1},
                        },
                        "candidates": {
                            "B1": {"symbols": ["TEST"]},
                            "B2": {"symbols": ["TEST"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            sources = root / "sources.json"
            calendar = root / "calendar.json"
            calendar.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "VERIFIED_POINT_IN_TIME_TRADING_CALENDAR",
                        "dates": [
                            "2026-01-05",
                            "2026-01-06",
                            "2026-01-07",
                            "2026-01-08",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            sources.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sources": [
                            {
                                "id": source_id,
                                "license_status": "PERMITTED_INTERNAL_RESEARCH",
                                "access_method": "synthetic_fixture",
                                "usage_boundary": "TEST_ONLY",
                                "fallback": "FAIL_CLOSED",
                                "last_success_at": "2026-01-03T15:00:00+08:00",
                                "allowed_uses": ["local_research_evaluation"],
                                "independence_group": source_id,
                                "license_snapshot_sha256": "a" * 64,
                            }
                            for source_id in ("test_a", "test_b")
                        ],
                    }
                ),
                encoding="utf-8",
            )
            universe = root / "universe.json"
            universe.write_text(
                json.dumps(
                    {
                        "listed_funds": [
                            {"symbol": "TEST"},
                            {"symbol": "OPERATIONAL_ONLY_NOT_IN_EVALUATION"},
                        ],
                        "open_end_funds": [],
                    }
                ),
                encoding="utf-8",
            )
            inputs: list[Path] = []
            for day, close in (("2026-01-05", 1.0), ("2026-01-06", 1.1), ("2026-01-07", 1.2), ("2026-01-08", 1.3)):
                path = root / f"{day}.json"
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "provenance": {"origin": "canonical"},
                            "run_date": day,
                            "as_of": f"{day}T15:00:00+08:00",
                            "is_trading_day": True,
                            "market_state": "closed",
                            "assets": [
                                {
                                    "symbol": "TEST",
                                    "raw_open": close - 0.01,
                                    "raw_close": close,
                                    "total_return_close": close,
                                    "open_observed_at": f"{day}T09:31:00+08:00",
                                    "close_observed_at": f"{day}T15:00:00+08:00",
                                    "source_ids": ["test_a", "test_b"],
                                    "open_source_ids": ["test_a", "test_b"],
                                    "close_source_ids": ["test_a", "test_b"],
                                    "trading_status": "OPEN",
                                    "corporate_actions": [],
                                    "license_status": "PERMITTED_INTERNAL_RESEARCH",
                                    "snapshot_sha256": "b" * 64,
                                    "history": [],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                inputs.append(path)
            result = audit_evaluation_readiness(
                inputs,
                sources_path=sources,
                universe_path=universe,
                manifest_path=manifest,
                calendar_path=calendar,
                test_only_allow_unprotected_protocol=True,
            )
            self.assertEqual(result["status"], "TEST_ONLY_READY_FOR_MECHANICAL_EVALUATION")
            self.assertEqual(result["reasons"], [])
            self.assertEqual(result["dataset"]["evaluation_symbols"], ["TEST"])
            self.assertEqual(result["dataset"]["universe_symbol_count"], 2)
            self.assertFalse(result["claim_boundary"]["strategy_returns_computed"])
            artifact = root / "readiness.json"
            write_readiness_artifact(artifact, result)
            self.assertEqual(verify_readiness_artifact(artifact)["status"], "INVALID")

    def test_caller_cannot_relax_manifest_minimum(self) -> None:
        with self.assertRaisesRegex(
            EvaluationDataError, "cannot relax the preregistered manifest"
        ):
            audit_evaluation_readiness(
                [],
                sources_path=ROOT / "config" / "sources.json",
                universe_path=ROOT / "config" / "universe.json",
                manifest_path=ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json",
                calendar_path=ROOT / "config" / "evaluation" / "trading-calendar.json",
                minimum_unique_dates=3,
            )

    def test_production_audit_rejects_substitute_protocol_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            substitute = Path(directory) / "weak.json"
            substitute.write_text(
                json.dumps({"dataset_contract": {"minimum_common_history_trading_days": 3}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                EvaluationDataError, "production readiness protocol path mismatch"
            ):
                audit_evaluation_readiness(
                    [],
                    sources_path=ROOT / "config" / "sources.json",
                    universe_path=ROOT / "config" / "universe.json",
                    manifest_path=substitute,
                    calendar_path=ROOT / "config" / "evaluation" / "trading-calendar.json",
                )

    def test_walk_forward_and_sealed_oos_are_date_disjoint(self) -> None:
        dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
        result = build_walk_forward_splits(
            dates,
            development_window=2,
            validation_window=1,
            test_window=1,
            step=1,
            sealed_oos_window=2,
            maximum_forward_label_horizon=0,
        )
        self.assertEqual(len(result["folds"]), 3)
        sealed = set(result["sealed_oos"]["ordered_dates"])
        for fold in result["folds"]:
            self.assertTrue(sealed.isdisjoint(fold["train"]))
            self.assertTrue(sealed.isdisjoint(fold["validation"]))
            self.assertTrue(sealed.isdisjoint(fold["test"]))

    def test_split_refuses_to_shrink_windows(self) -> None:
        with self.assertRaisesRegex(
            EvaluationDataError, "INSUFFICIENT_DISTINCT_TRADING_DATES"
        ):
            build_walk_forward_splits(
                ["2026-01-01", "2026-01-02", "2026-01-03"],
                development_window=1,
                validation_window=1,
                test_window=1,
                step=1,
                sealed_oos_window=1,
                maximum_forward_label_horizon=0,
            )


if __name__ == "__main__":
    unittest.main()
