from __future__ import annotations

from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.intraday_capture import capture_intraday_snapshot
from vibe_finance.open_capture import OpenCaptureError, SHANGHAI
from vibe_finance.pipeline import validate_snapshot


ROOT = Path(__file__).resolve().parents[1]


def _tencent_payload(prices: dict[str, tuple[str, str, str]]) -> bytes:
    lines = []
    for symbol, (name, previous_close, current_price) in prices.items():
        fields = [""] * 31
        fields[0] = "1"
        fields[1] = name
        fields[2] = symbol
        fields[3] = current_price
        fields[4] = previous_close
        fields[5] = previous_close
        fields[6] = "100"
        fields[30] = "20260727130005"
        prefix = "sh" if symbol[0] in {"5", "6"} else "sz"
        lines.append(f'v_{prefix}{symbol}="{"~".join(fields)}";')
    return "\n".join(lines).encode("gb18030")


def _sina_payload(prices: dict[str, tuple[str, str, str]]) -> bytes:
    lines = []
    for symbol, (name, previous_close, current_price) in prices.items():
        fields = [""] * 33
        fields[0] = name
        fields[1] = previous_close
        fields[2] = previous_close
        fields[3] = current_price
        fields[8] = "10000"
        fields[30] = "2026-07-27"
        fields[31] = "13:00:06"
        prefix = "sh" if symbol[0] in {"5", "6"} else "sz"
        lines.append(f'var hq_str_{prefix}{symbol}="{",".join(fields)}";')
    return "\n".join(lines).encode("gb18030")


class IntradayCaptureTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, dict[str, tuple[str, str, str]]]:
        strategy = json.loads((ROOT / "config/strategy.json").read_text(encoding="utf-8"))
        strategy_path = root / "strategy.json"
        strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
        prices = {
            "510300": ("沪深300ETF", "4.700", "4.690"),
            "518880": ("黄金ETF", "8.560", "8.570"),
            "511010": ("国债ETF", "140.900", "140.910"),
            "511880": ("现金ETF", "100.640", "100.641"),
        }
        types = {
            "510300": ("equity_etf", "core_equity", "csi300"),
            "518880": ("gold_etf", "gold", "gold"),
            "511010": ("bond_etf", "fixed_income", "government_bond_5y"),
            "511880": ("cash_etf", "cash_management", "cash_management"),
        }
        listed = []
        assets = []
        for symbol, (asset_type, bucket, group) in types.items():
            item = {
                "symbol": symbol,
                "name": prices[symbol][0],
                "asset_type": asset_type,
                "risk_bucket": bucket,
                "exposure_group": group,
                "primary_source_ids": ["sse_etf", "fund_company"],
                "order_engine": "next_open",
            }
            listed.append(item)
            assets.append(
                {
                    **item,
                    "close": float(prices[symbol][1]),
                    "daily_return": 0.0,
                    "lot_size": 100,
                    "history": [],
                    "source_ids": ["eastmoney", "sse_etf", "fund_company"],
                    "price_source_ids": ["eastmoney", "sse_etf"],
                    "price_as_of": "2026-07-24T15:00:00+08:00",
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
                    "run_date": "2026-07-27",
                    "as_of": "2026-07-27T08:00:00+08:00",
                    "is_trading_day": True,
                    "market_state": "preopen",
                    "simulation_only": True,
                    "indices": [],
                    "assets": assets,
                    "evidence": [{"title": "fixture", "url": "https://example.invalid", "as_of": "2026-07-27T08:00:00+08:00", "tier": "TEST"}],
                }
            ),
            encoding="utf-8",
        )
        ledger_path = root / "portfolio.json"
        ledger_path.write_text(
            json.dumps(
                {
                    "positions": {},
                    "pending_orders": [{"symbol": "510300", "status": "PENDING_NEXT_OPEN"}],
                }
            ),
            encoding="utf-8",
        )
        return strategy_path, universe_path, base_path, ledger_path, prices

    def test_capture_seals_conservative_intraday_prices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path, prices = self._fixture(root)

            def fetch(source_id: str, url: str, timeout: float) -> bytes:
                return _tencent_payload(prices) if source_id == "tencent_finance" else _sina_payload(prices)

            output = root / "intraday.json"
            result = capture_intraday_snapshot(
                base_snapshot_path=base_path,
                output_path=output,
                strategy_path=strategy_path,
                universe_path=universe_path,
                ledger_path=ledger_path,
                now=datetime(2026, 7, 27, 13, 0, 10, tzinfo=SHANGHAI),
                fetch=fetch,
            )
            self.assertEqual(result["status"], "SEALED")
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["market_state"], "continuous_trading")
            self.assertEqual({asset["asset_type"] for asset in snapshot["assets"]}, {"equity_etf", "gold_etf", "bond_etf", "cash_etf"})
            target = next(asset for asset in snapshot["assets"] if asset["symbol"] == "510300")
            self.assertEqual(target["execution_price"], 4.69)
            self.assertEqual(target["trading_status"], "TRADING")
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
            self.assertEqual(
                validate_snapshot(
                    snapshot,
                    strategy,
                    now=datetime(2026, 7, 27, 13, 1, tzinfo=SHANGHAI),
                ),
                [],
            )

    def test_capture_refuses_outside_intraday_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path, prices = self._fixture(root)

            def fetch(source_id: str, url: str, timeout: float) -> bytes:
                return _tencent_payload(prices) if source_id == "tencent_finance" else _sina_payload(prices)

            output = root / "intraday.json"
            with self.assertRaises(OpenCaptureError):
                capture_intraday_snapshot(
                    base_snapshot_path=base_path,
                    output_path=output,
                    strategy_path=strategy_path,
                    universe_path=universe_path,
                    ledger_path=ledger_path,
                    now=datetime(2026, 7, 27, 12, 59, 59, tzinfo=SHANGHAI),
                    fetch=fetch,
                )
            self.assertFalse(output.exists())

    def test_capture_retries_transient_cross_source_refresh_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strategy_path, universe_path, base_path, ledger_path, prices = self._fixture(root)
            calls = {"sina_finance": 0}
            delays: list[float] = []

            def fetch(source_id: str, url: str, timeout: float) -> bytes:
                if source_id == "tencent_finance":
                    return _tencent_payload(prices)
                calls["sina_finance"] += 1
                current = dict(prices)
                if calls["sina_finance"] == 1:
                    current["510300"] = ("沪深300ETF", "4.700", "4.700")
                return _sina_payload(current)

            output = root / "intraday.json"
            result = capture_intraday_snapshot(
                base_snapshot_path=base_path,
                output_path=output,
                strategy_path=strategy_path,
                universe_path=universe_path,
                ledger_path=ledger_path,
                now=datetime(2026, 7, 27, 13, 0, 10, tzinfo=SHANGHAI),
                fetch=fetch,
                sleeper=delays.append,
            )
            self.assertEqual(result["status"], "SEALED")
            self.assertEqual(calls["sina_finance"], 2)
            self.assertEqual(delays, [0.5])


if __name__ == "__main__":
    unittest.main()
