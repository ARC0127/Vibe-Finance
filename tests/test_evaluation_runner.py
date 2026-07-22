from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from vibe_finance.candidate_strategies import CANDIDATE_SYMBOLS
from vibe_finance.evaluation_runner import (
    EVIDENCE_CLASS,
    EvaluationRunnerError,
    run_synthetic_mechanical_evaluation,
    verify_synthetic_evaluation_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json"


def _asset(
    symbol: str,
    close: float,
    history: list[float],
    risk_bucket: str,
    exposure_group: str,
    *,
    asset_type: str = "equity_etf",
) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "asset_type": asset_type,
        "risk_bucket": risk_bucket,
        "exposure_group": exposure_group,
        "close": close,
        "daily_return": close / history[-2] - 1.0,
        "history": history[:-1] + [close],
        "history_adjusted_for_corporate_actions": True,
        "corporate_actions": [],
        "source_ids": ["source_a", "source_b", "eastmoney"],
        "trading_status": "TRADING",
        "lot_size": 100,
    }


def _b0_snapshot(signal_date: str) -> dict:
    return {
        "schema_version": 1,
        "run_date": signal_date,
        "as_of": f"{signal_date}T15:00:00+08:00",
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
            _asset(
                "510300",
                9.9,
                [8.0 + 0.1 * index for index in range(20)],
                "core_equity",
                "csi300",
            ),
            _asset(
                "512100",
                9.95,
                [9.0 + 0.05 * index for index in range(20)],
                "small_growth_equity",
                "csi1000",
            ),
            _asset(
                "510880",
                10.0,
                [10.0] * 20,
                "dividend_equity",
                "dividend",
            ),
            _asset(
                "518880",
                10.0,
                [10.0] * 20,
                "gold",
                "gold",
                asset_type="gold_etf",
            ),
        ],
        "evidence": [],
    }


def _opens(execute_date: str, price: float) -> dict:
    return {
        symbol: {
            "trading_status": "TRADING",
            "corporate_actions": [],
            "observations": [
                {
                    "value": price,
                    "observed_at": f"{execute_date}T09:31:00+08:00",
                    "source_id": source_id,
                    "raw_content_sha256": character * 64,
                }
                for source_id, character in (
                    ("source_a", "a"),
                    ("source_b", "b"),
                )
            ],
        }
        for symbol in CANDIDATE_SYMBOLS
    }


def _synthetic_input() -> dict:
    dates = [
        (date(2025, 1, 1) + timedelta(days=index)).isoformat()
        for index in range(298)
    ]
    prices = {
        symbol: [
            10.0
            * math.exp(
                (0.00045 + symbol_index * 0.00008) * day
                + 0.003 * math.sin(day * (0.17 + symbol_index * 0.013))
            )
            * (0.3 if day >= 275 else 1.0)
            for day in range(297)
        ]
        for symbol_index, symbol in enumerate(CANDIDATE_SYMBOLS)
    }
    rows = [
        {
            "date": dates[index],
            **{symbol: prices[symbol][index] for symbol in CANDIDATE_SYMBOLS},
        }
        for index in range(297)
    ]
    signal_closes = {
        "510300": 9.9,
        "510880": 10.0,
        "512100": 9.95,
        "518880": 10.0,
    }
    cycles = []
    for signal_index, open_price in ((273, 10.0), (296, 9.0)):
        signal_date = dates[signal_index]
        execute_date = dates[signal_index + 1]
        cycles.append(
            {
                "signal_date": signal_date,
                "execute_date": execute_date,
                "execution_cutoff": f"{execute_date}T09:35:00+08:00",
                "signal_closes": signal_closes,
                "b0_snapshot": _b0_snapshot(signal_date),
                "open_observations": _opens(execute_date, open_price),
            }
        )
    return {
        "schema_version": 1,
        "evidence_class": EVIDENCE_CLASS,
        "initial_cash_cny": 29900.0,
        "readiness_artifact_sha256": "c" * 64,
        "trading_calendar_dates": dates,
        "point_in_time_rows": rows,
        "source_registry": {
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
        },
        "cycles": cycles,
    }


def _write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )


class EvaluationRunnerTests(unittest.TestCase):
    def test_entry_run_save_reload_eval_is_mechanical_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "synthetic-input.json"
            artifact_path = root / "synthetic-artifact.json"
            _write(input_path, _synthetic_input())

            result = run_synthetic_mechanical_evaluation(
                input_path,
                repo_root=ROOT,
                manifest_path=MANIFEST,
                output_path=artifact_path,
            )

            self.assertEqual(result["evidence_class"], EVIDENCE_CLASS)
            self.assertIsNone(result["metrics"])
            self.assertIsNone(result["ranking"])
            self.assertIsNone(result["strategy_ranking"])
            self.assertFalse(result["promotion_authorized"])
            self.assertEqual(result["cost_scenarios_bps"], [10, 25, 50])
            for candidate in ("B0", "B1", "B2", "B3"):
                candidate_result = result["candidates"][candidate]
                self.assertIsNone(candidate_result["metrics"])
                self.assertEqual(candidate_result["evidence_class"], EVIDENCE_CLASS)
                self.assertIsNone(candidate_result["ranking"])
                self.assertFalse(candidate_result["promotion_authorized"])
                self.assertEqual(
                    set(candidate_result["cost_scenarios"]), {"10", "25", "50"}
                )
                for scenario in candidate_result["cost_scenarios"].values():
                    self.assertIsNone(scenario["metrics"])
                    self.assertEqual(scenario["evidence_class"], EVIDENCE_CLASS)
                    self.assertIsNone(scenario["ranking"])
                    self.assertFalse(scenario["promotion_authorized"])
                    self.assertEqual(len(scenario["cycles"]), 2)
                    first, second = scenario["cycles"]
                    self.assertEqual(first["evidence_class"], EVIDENCE_CLASS)
                    self.assertFalse(
                        first["plan"]["claim_boundary"]["next_open_observed"]
                    )
                    for symbol, quantity in first["state_after"]["quantities"].items():
                        if quantity > 0:
                            accounting = first["state_after"]["position_accounting"][
                                symbol
                            ]
                            self.assertEqual(
                                accounting["last_buy_date"], first["execute_date"]
                            )
                            self.assertEqual(
                                second["plan"]["pre_state"][
                                    "available_to_sell_quantities"
                                ][symbol],
                                quantity,
                            )
                    if candidate == "B3":
                        self.assertEqual(
                            first["signal"]["candidate_proposal_sha256"],
                            first["plan"]["candidate_proposal_sha256"],
                        )
            b1_base = result["candidates"]["B1"]["cost_scenarios"]["10"]
            self.assertNotEqual(
                b1_base["cycles"][1]["execution"]["after_state"][
                    "realized_pnl_cny"
                ],
                0.0,
            )
            self.assertEqual(
                b1_base["final_state"]["cumulative_realized_pnl_cny"],
                b1_base["cycles"][1]["execution"]["after_state"][
                    "realized_pnl_cny"
                ],
            )

            verified = verify_synthetic_evaluation_artifact(
                input_path,
                artifact_path,
                repo_root=ROOT,
                manifest_path=MANIFEST,
            )
            self.assertEqual(
                verified["status"],
                "VERIFIED_TEST_SYNTHETIC_MECHANICAL_CLOSED_LOOP",
            )
            self.assertEqual(
                verified["replayed_artifact_sha256"], result["artifact_sha256"]
            )
            self.assertIsNone(verified["metrics"])
            self.assertIsNone(verified["ranking"])
            self.assertFalse(verified["promotion_authorized"])
            with self.assertRaisesRegex(EvaluationRunnerError, "already exists"):
                run_synthetic_mechanical_evaluation(
                    input_path,
                    repo_root=ROOT,
                    manifest_path=MANIFEST,
                    output_path=artifact_path,
                )

    def test_future_open_changes_execution_but_not_same_cycle_signal_or_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_input = _synthetic_input()
            second_input = copy.deepcopy(first_input)
            for observation in second_input["cycles"][0]["open_observations"].values():
                for source_value in observation["observations"]:
                    source_value["value"] = 9.5
            _write(first_path, first_input)
            _write(second_path, second_input)

            first = run_synthetic_mechanical_evaluation(
                first_path, repo_root=ROOT, manifest_path=MANIFEST
            )
            second = run_synthetic_mechanical_evaluation(
                second_path, repo_root=ROOT, manifest_path=MANIFEST
            )
            self.assertNotEqual(first["input_binding"], second["input_binding"])
            for candidate in ("B0", "B1", "B2", "B3"):
                first_cycle = first["candidates"][candidate]["cost_scenarios"]["10"][
                    "cycles"
                ][0]
                second_cycle = second["candidates"][candidate]["cost_scenarios"][
                    "10"
                ]["cycles"][0]
                self.assertEqual(first_cycle["signal"], second_cycle["signal"])
                self.assertEqual(first_cycle["plan"], second_cycle["plan"])
                self.assertNotEqual(
                    first_cycle["execution"]["after_state"],
                    second_cycle["execution"]["after_state"],
                )

    def test_reload_fails_closed_after_bound_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            artifact_path = root / "artifact.json"
            payload = _synthetic_input()
            _write(input_path, payload)
            run_synthetic_mechanical_evaluation(
                input_path,
                repo_root=ROOT,
                manifest_path=MANIFEST,
                output_path=artifact_path,
            )
            payload["cycles"][0]["open_observations"]["510300"]["observations"][
                0
            ]["value"] = 9.75
            _write(input_path, payload)
            invalid = verify_synthetic_evaluation_artifact(
                input_path,
                artifact_path,
                repo_root=ROOT,
                manifest_path=MANIFEST,
            )
            self.assertEqual(invalid["status"], "INVALID")
            self.assertIn("INPUT_CONTENT_HASH_MISMATCH", invalid["mismatches"])
            self.assertIsNone(invalid["metrics"])
            self.assertFalse(invalid["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
