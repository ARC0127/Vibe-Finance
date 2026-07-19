from __future__ import annotations

from datetime import datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.pipeline import DataGateError, initialize_ledger, project_status, run_pipeline, validate_snapshot


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = ROOT / "config" / "strategy.json"


def snapshot(run_date: str, *, trading: bool, shock: bool = False, with_open: bool = False) -> dict:
    prices = [100.0 + index * 0.001 for index in range(20)]
    asset = {
        "symbol": "511880",
        "name": "银华日利ETF",
        "asset_type": "cash_etf",
        "close": prices[-1],
        "daily_return": 0.00001,
        "lot_size": 100,
        "history": prices,
        "source_ids": ["source_a", "source_b"],
    }
    if with_open:
        asset["open"] = 100.02
    return {
        "schema_version": 1,
        "run_date": run_date,
        "as_of": f"{run_date}T15:00:00+08:00",
        "is_trading_day": trading,
        "market_state": "closed" if trading else "weekend",
        "indices": [
            {
                "symbol": "000001",
                "name": "上证指数",
                "close": 3800.0,
                "daily_return": -0.04 if shock else 0.001,
                "broad": True,
                "source_ids": ["source_a", "source_b"],
            }
        ],
        "assets": [asset],
        "evidence": [
            {"title": "fixture", "url": "https://example.invalid", "as_of": run_date, "tier": "TEST"}
        ],
    }


class PipelineTests(unittest.TestCase):
    def test_initialize_reserves_research_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "portfolio.json"
            initialize_ledger(ledger)
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(value["cash_cny"], 29900.0)
            self.assertEqual(value["research_infrastructure"]["reserved_cny"], 100.0)
            self.assertEqual(value["research_infrastructure"]["actual_calls"], 0)

    def test_future_snapshot_is_rejected(self) -> None:
        strategy = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        value = snapshot("2999-01-01", trading=False)
        with self.assertRaises(DataGateError):
            validate_snapshot(value, strategy)

    def test_non_trading_and_shock_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(snapshot("2026-07-19", trading=False, shock=True)), encoding="utf-8")
            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["new_orders"], 0)
            self.assertIn("BROAD_MARKET_SHOCK", result["blocks"])
            self.assertIn("NON_TRADING_DAY", result["blocks"])

    def test_status_is_read_only_and_detects_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(snapshot("2026-07-19", trading=False)), encoding="utf-8")
            run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )
            heartbeat = root / "heartbeat.json"
            before = heartbeat.read_bytes()
            last = json.loads(before)["last_success_at"]
            current = datetime.fromisoformat(last) + timedelta(hours=40)
            result = project_status(root / "portfolio.json", root / "reports", 36.0, current)
            self.assertEqual(result["status"], "STALE")
            self.assertEqual(before, heartbeat.read_bytes())

    def test_signal_is_filled_only_at_later_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(snapshot("2026-07-17", trading=True)), encoding="utf-8")
            second.write_text(json.dumps(snapshot("2026-07-18", trading=True, with_open=True)), encoding="utf-8")
            kwargs = {
                "ledger_path": root / "portfolio.json",
                "strategy_path": STRATEGY_PATH,
                "report_dir": root / "reports",
                "orders_log": root / "orders.jsonl",
            }
            first_result = run_pipeline(input_path=first, **kwargs)
            self.assertEqual(first_result["new_orders"], 1)
            first_ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            self.assertEqual(first_ledger["positions"], {})
            self.assertEqual(len(first_ledger["pending_orders"]), 1)

            second_result = run_pipeline(input_path=second, **kwargs)
            self.assertEqual(second_result["fills"], 1)
            second_ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            self.assertEqual(second_ledger["positions"]["511880"]["quantity"], 100)
            self.assertLess(second_ledger["cash_cny"], 29900.0)


if __name__ == "__main__":
    unittest.main()
