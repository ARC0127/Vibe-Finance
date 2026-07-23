from __future__ import annotations

from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.open_capture import OpenCaptureError, SHANGHAI, capture_open_snapshot
from vibe_finance.pipeline import DataGateError, validate_snapshot


ROOT = Path(__file__).resolve().parents[1]


def _tencent_payload(prices: dict[str, tuple[str, str, str]]) -> bytes:
    lines = []
    for symbol, (name, previous_close, open_price) in prices.items():
        fields = [""] * 31
        fields[0] = "1"
        fields[1] = name
        fields[2] = symbol
        fields[3] = open_price
        fields[4] = previous_close
        fields[5] = open_price
        fields[6] = "100"
        fields[30] = "20260723093005"
        lines.append(f'v_sh{symbol}="{"~".join(fields)}";')
    return "\n".join(lines).encode("gb18030")


def _sina_payload(prices: dict[str, tuple[str, str, str]]) -> bytes:
    lines = []
    for symbol, (name, previous_close, open_price) in prices.items():
        fields = [""] * 33
        fields[0] = name
        fields[1] = open_price
        fields[2] = previous_close
        fields[3] = open_price
        fields[8] = "10000"
        fields[30] = "2026-07-23"
        fields[31] = "09:30:06"
        lines.append(f'var hq_str_sh{symbol}="{",".join(fields)}";')
    return "\n".join(lines).encode("gb18030")


class OpenCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = {
            "518880": ("黄金ETF华安", "8.560", "8.604"),
            "511010": ("国债ETF国泰", "140.899", "140.900"),
            "511880": ("银华日利ETF", "100.642", "100.638"),
        }

    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        strategy = json.loads((ROOT / "config/strategy.json").read_text(encoding="utf-8"))
        strategy_path = root / "strategy.json"
        strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
        listed = []
        base_assets = []
        types = {
            "518880": ("gold_etf", "gold", "gold", "huaan"),
            "511010": ("bond_etf", "fixed_income", "government_bond_5y", "guotai_fund"),
            "511880": ("cash_etf", "cash_management", "cash_management", "yinhua"),
        }
        for symbol, (asset_type, bucket, group, manager) in types.items():
            listed.append(
                {
                    "symbol": symbol,
                    "name": self.prices[symbol][0],
                    "asset_type": asset_type,
                    "risk_bucket": bucket,
                    "exposure_group": group,
                    "primary_source_ids": ["sse_etf", manager],
                    "order_engine": "next_open",
                }
            )
            base_assets.append(
                {
                    **listed[-1],
                    "close": float(self.prices[symbol][1]),
                    "daily_return": 0.0,
                    "lot_size": 100,
                    "history": [],
                    "source_ids": ["eastmoney", "sse_etf", manager],
                    "price_source_ids": ["eastmoney", "sse_etf"],
                    "price_as_of": "2026-07-22T15:00:00+08:00",
                    "trading_status": "UNVERIFIED_PREOPEN",
                    "security_identity_status": f"VERIFIED_ETF_{symbol}",
                    "st_delisting_status": "ETF_NOT_ST_AND_NO_TERMINATION_EVIDENCE_AT_CUTOFF",
                    "corporate_action_status": "CLEARED",
                    "corporate_actions": [],
                    "history_adjusted_for_corporate_actions": True,
                }
            )
        universe_path = root / "universe.json"
        universe_path.write_text(json.dumps({"listed_funds": listed}), encoding="utf-8")
        base_path = root / "preopen.json"
        base_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "run_date": "2026-07-23",
                    "as_of": "2026-07-23T08:00:00+08:00",
                    "is_trading_day": True,
                    "market_state": "preopen",
                    "simulation_only": True,
                    "indices": [],
                    "assets": base_assets,
                    "evidence": [
                        {
                            "title": "fixture",
                            "url": "https://example.invalid",
                            "as_of": "2026-07-23T08:00:00+08:00",
                            "tier": "TEST",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        ledger_path = root / "portfolio.json"
        ledger_path.write_text(
            json.dumps({"positions": {}, "pending_orders": []}), encoding="utf-8"
        )
        return strategy_path, universe_path, base_path, ledger_path

    def _fetch(self, source_id: str, url: str, timeout: float) -> bytes:
        self.assertIn("511010", url)
        self.assertLessEqual(timeout, 10.0)
        if source_id == "tencent_finance":
            return _tencent_payload(self.prices)
        if source_id == "sina_finance":
            return _sina_payload(self.prices)
        raise AssertionError(source_id)

    def test_capture_seals_all_defensive_types_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path = self._fixture(root)
            output = root / "open.json"
            result = capture_open_snapshot(
                base_snapshot_path=base_path,
                output_path=output,
                strategy_path=strategy_path,
                universe_path=universe_path,
                ledger_path=ledger_path,
                now=datetime(2026, 7, 23, 9, 30, 10, tzinfo=SHANGHAI),
                fetch=self._fetch,
            )
            self.assertEqual(result["status"], "SEALED")
            self.assertEqual(result["symbols"], ["511010", "511880", "518880"])
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                {asset["asset_type"] for asset in snapshot["assets"]},
                {"gold_etf", "bond_etf", "cash_etf"},
            )
            self.assertTrue(
                all(
                    asset["open_source_ids"] == ["tencent_finance", "sina_finance"]
                    and asset["trading_status"] == "TRADING"
                    for asset in snapshot["assets"]
                )
            )
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
            self.assertEqual(
                validate_snapshot(
                    snapshot,
                    strategy,
                    now=datetime(2026, 7, 23, 9, 30, 10, tzinfo=SHANGHAI),
                ),
                [],
            )
            with self.assertRaisesRegex(OpenCaptureError, "overwrite"):
                capture_open_snapshot(
                    base_snapshot_path=base_path,
                    output_path=output,
                    strategy_path=strategy_path,
                    universe_path=universe_path,
                    ledger_path=ledger_path,
                    now=datetime(2026, 7, 23, 9, 30, 10, tzinfo=SHANGHAI),
                    fetch=self._fetch,
                )

    def test_next_day_snapshot_missing_defensive_type_fails_validation(self) -> None:
        strategy = json.loads((ROOT / "config/strategy.json").read_text(encoding="utf-8"))
        snapshot = {
            "schema_version": 2,
            "run_date": "2026-07-24",
            "as_of": "2026-07-24T08:00:00+08:00",
            "is_trading_day": True,
            "market_state": "preopen",
            "indices": [],
            "assets": [],
            "evidence": [],
        }
        with self.assertRaisesRegex(DataGateError, "交易日快照缺少必需资产类型"):
            validate_snapshot(
                snapshot,
                strategy,
                now=datetime(2026, 7, 24, 8, 1, tzinfo=SHANGHAI),
            )

    def test_capture_refuses_outside_window_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path = self._fixture(root)
            output = root / "open.json"
            with self.assertRaisesRegex(OpenCaptureError, "outside"):
                capture_open_snapshot(
                    base_snapshot_path=base_path,
                    output_path=output,
                    strategy_path=strategy_path,
                    universe_path=universe_path,
                    ledger_path=ledger_path,
                    now=datetime(2026, 7, 23, 9, 35, 1, tzinfo=SHANGHAI),
                    fetch=self._fetch,
                )
            self.assertFalse(output.exists())

    def test_capture_refuses_price_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path = self._fixture(root)
            output = root / "open.json"

            def conflicting_fetch(source_id: str, url: str, timeout: float) -> bytes:
                if source_id == "tencent_finance":
                    return _tencent_payload(self.prices)
                conflicting = dict(self.prices)
                conflicting["511880"] = ("银华日利ETF", "100.642", "100.639")
                return _sina_payload(conflicting)

            with self.assertRaisesRegex(OpenCaptureError, "open price conflict: 511880"):
                capture_open_snapshot(
                    base_snapshot_path=base_path,
                    output_path=output,
                    strategy_path=strategy_path,
                    universe_path=universe_path,
                    ledger_path=ledger_path,
                    now=datetime(2026, 7, 23, 9, 30, 10, tzinfo=SHANGHAI),
                    fetch=conflicting_fetch,
                )
            self.assertFalse(output.exists())

    def test_pending_order_requires_primary_identity_and_action_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path = self._fixture(root)
            base = json.loads(base_path.read_text(encoding="utf-8"))
            target = next(asset for asset in base["assets"] if asset["symbol"] == "511880")
            target.pop("security_identity_status")
            target["corporate_action_status"] = "UNVERIFIED_PREOPEN"
            base_path.write_text(json.dumps(base), encoding="utf-8")
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "pending_orders": [
                            {"symbol": "511880", "status": "PENDING_NEXT_OPEN"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "open.json"
            capture_open_snapshot(
                base_snapshot_path=base_path,
                output_path=output,
                strategy_path=strategy_path,
                universe_path=universe_path,
                ledger_path=ledger_path,
                now=datetime(2026, 7, 23, 9, 30, 10, tzinfo=SHANGHAI),
                fetch=self._fetch,
            )
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            captured = next(asset for asset in snapshot["assets"] if asset["symbol"] == "511880")
            self.assertEqual(
                captured["trading_status"],
                "UNVERIFIED_SECURITY_IDENTITY_AND_CORPORATE_ACTION",
            )
            self.assertEqual(
                captured["quality"], "OPEN_MATCH_BUT_PRIMARY_GATES_NOT_CLEARED"
            )


if __name__ == "__main__":
    unittest.main()
