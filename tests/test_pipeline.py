from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
import tempfile
import unittest
from pathlib import Path

from vibe_finance.pipeline import (
    DataGateError,
    _trade_fees,
    initialize_ledger,
    project_status,
    run_fund_nav_pipeline,
    run_pipeline,
    settle_open_orders,
    validate_snapshot,
)


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
        "source_ids": ["source_a", "eastmoney"],
    }
    if with_open:
        asset["open"] = 100.02
        asset["open_source_ids"] = ["open_source_a", "open_source_b"]
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


def fund_snapshot(run_date: str, nav: float) -> dict:
    value = snapshot(run_date, trading=True)
    value["as_of"] = f"{run_date}T22:30:00+08:00"
    value["market_state"] = "nav_published"
    asset = value["assets"][0]
    asset.update(
        {
            "symbol": "110037",
            "name": "易方达纯债债券A",
            "asset_type": "open_end_bond_fund",
            "close": nav,
            "history": [nav - 0.019 + index * 0.001 for index in range(20)],
            "source_ids": ["eastmoney", "efunds"],
            "risk_bucket": "fixed_income",
            "exposure_group": "long_duration_bond",
            "order_engine": "next_confirmed_nav",
            "fund_metadata": {
                "nav_date": run_date,
                "nav_age_trading_days": 0,
                "subscription_status": "OPEN",
                "redemption_status": "OPEN",
                "aum_cny": 1_700_000_000,
                "fees_verified": True,
                "purchase_fee_rate": 0.0008,
                "redemption_fee_rate": 0.015,
            },
        }
    )
    return value


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

    def test_open_settlement_fills_without_creating_new_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            close_input = root / "close.json"
            open_input = root / "open.json"
            close_input.write_text(
                json.dumps(snapshot("2026-07-17", trading=True)), encoding="utf-8"
            )
            opening = snapshot("2026-07-18", trading=True, with_open=True)
            opening["as_of"] = "2026-07-18T09:35:00+08:00"
            opening["market_state"] = "open"
            open_input.write_text(json.dumps(opening), encoding="utf-8")
            ledger = root / "portfolio.json"
            orders_log = root / "orders.jsonl"

            close_result = run_pipeline(
                input_path=close_input,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "daily",
                orders_log=orders_log,
            )
            self.assertEqual(close_result["new_orders"], 1)

            open_result = settle_open_orders(
                input_path=open_input,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "execution",
                orders_log=orders_log,
            )
            self.assertEqual(open_result["filled"], 1)
            self.assertEqual(open_result["pending_after"], 0)
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(value["positions"]["511880"]["quantity"], 100)
            self.assertEqual(value["run_history"][-1]["mode"], "open_settlement")
            event = json.loads(
                (root / "execution" / "2026-07-18-open.json").read_text(
                    encoding="utf-8"
                )
            )["events"][0]
            self.assertEqual(event["commission_cny"], 5.0)
            self.assertEqual(event["stamp_tax_cny"], 0.0)
            self.assertEqual(event["transfer_fee_cny"], 0.0)

    def test_stock_fee_model_is_side_aware(self) -> None:
        strategy = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        buy = _trade_fees(Decimal("100000"), "stock", "BUY", strategy)
        sell = _trade_fees(Decimal("100000"), "stock", "SELL", strategy)
        etf_sell = _trade_fees(Decimal("100000"), "equity_etf", "SELL", strategy)

        self.assertEqual(buy["commission_cny"], Decimal("30.00"))
        self.assertEqual(buy["transfer_fee_cny"], Decimal("1.00"))
        self.assertEqual(buy["stamp_tax_cny"], Decimal("0.00"))
        self.assertEqual(sell["total_fees_cny"], Decimal("81.00"))
        self.assertEqual(etf_sell["total_fees_cny"], Decimal("30.00"))

    def test_open_settlement_cancels_unadjusted_corporate_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            close_input = root / "close.json"
            open_input = root / "open.json"
            close_input.write_text(
                json.dumps(snapshot("2026-07-17", trading=True)), encoding="utf-8"
            )
            ledger = root / "portfolio.json"
            run_pipeline(
                input_path=close_input,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "daily",
                orders_log=root / "orders.jsonl",
            )
            opening = snapshot("2026-07-18", trading=True, with_open=True)
            opening["as_of"] = "2026-07-18T09:35:00+08:00"
            opening["market_state"] = "open"
            opening["assets"][0]["corporate_actions"] = [
                {
                    "date": "2026-07-18",
                    "type": "cash_dividend",
                    "source_ids": ["exchange"],
                }
            ]
            opening["assets"][0]["history_adjusted_for_corporate_actions"] = False
            open_input.write_text(json.dumps(opening), encoding="utf-8")

            result = settle_open_orders(
                input_path=open_input,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "execution",
                orders_log=root / "orders.jsonl",
            )

            self.assertEqual(result["filled"], 0)
            self.assertEqual(result["cancelled"], 1)
            self.assertEqual(result["pending_after"], 0)
            event = json.loads(
                (root / "execution" / "2026-07-18-open.json").read_text(
                    encoding="utf-8"
                )
            )["events"][0]
            self.assertEqual(
                event["cancellation_reason"], "UNADJUSTED_CORPORATE_ACTION"
            )

    def test_unadjusted_corporate_action_blocks_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-17", trading=True)
            asset = value["assets"][0]
            asset["asset_type"] = "equity_etf"
            asset["history"] = [price / 100 for price in asset["history"]]
            asset["close"] = asset["history"][-1]
            asset["corporate_actions"] = [
                {
                    "date": "2026-07-15",
                    "type": "cash_dividend",
                    "cash_per_share": 0.149,
                    "source_ids": ["sse"],
                }
            ]
            asset["history_adjusted_for_corporate_actions"] = False
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")

            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )

            decision = json.loads(
                (root / "reports" / "2026-07-17-short.json").read_text(
                    encoding="utf-8"
                )
            )
            recommendation = decision["recommendations"][0]
            self.assertEqual(result["new_orders"], 0)
            self.assertEqual(recommendation["action"], "WATCH")
            self.assertIn("UNADJUSTED_CORPORATE_ACTION", recommendation["reasons"])

    def test_adjusted_corporate_action_can_reach_normal_signal_logic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-17", trading=True)
            asset = value["assets"][0]
            asset["asset_type"] = "equity_etf"
            asset["history"] = [price / 100 for price in asset["history"]]
            asset["close"] = asset["history"][-1]
            asset["corporate_actions"] = [
                {
                    "date": "2026-07-15",
                    "type": "cash_dividend",
                    "cash_per_share": 0.149,
                    "source_ids": ["sse"],
                }
            ]
            asset["history_adjusted_for_corporate_actions"] = True
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")

            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )

            decision = json.loads(
                (root / "reports" / "2026-07-17-short.json").read_text(
                    encoding="utf-8"
                )
            )
            recommendation = decision["recommendations"][0]
            self.assertEqual(result["new_orders"], 1)
            self.assertEqual(recommendation["action"], "BUY")
            self.assertNotIn(
                "UNADJUSTED_CORPORATE_ACTION", recommendation["reasons"]
            )

    def test_fund_requires_tiantian_crosscheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-17", trading=True)
            value["assets"][0]["source_ids"] = ["source_a", "source_b"]
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")

            run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )

            decision = json.loads(
                (root / "reports" / "2026-07-17-short.json").read_text(encoding="utf-8")
            )
            item = decision["recommendations"][0]
            self.assertEqual(item["action"], "WATCH")
            self.assertIn("TIANTIAN_CROSSCHECK_MISSING", item["reasons"])

    def test_open_end_fund_without_supported_nav_contract_stays_watch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-17", trading=True)
            asset = value["assets"][0]
            asset["asset_type"] = "open_end_bond_fund"
            asset["source_ids"] = ["eastmoney", "efunds"]
            asset["fund_metadata"] = {
                "nav_date": "2026-07-17",
                "nav_age_trading_days": 0,
                "subscription_status": "OPEN",
                "redemption_status": "OPEN",
                "aum_cny": 1_000_000_000,
                "fees_verified": True,
            }
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")

            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )

            self.assertEqual(result["new_orders"], 0)
            decision = json.loads(
                (root / "reports" / "2026-07-17-short.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "OPEN_END_FUND_EXECUTION_NOT_IMPLEMENTED",
                decision["recommendations"][0]["reasons"],
            )

    def test_open_end_fund_uses_later_confirmed_nav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(fund_snapshot("2026-07-17", 1.1157)), encoding="utf-8")
            second.write_text(json.dumps(fund_snapshot("2026-07-18", 1.1160)), encoding="utf-8")
            kwargs = {
                "ledger_path": root / "portfolio.json",
                "strategy_path": STRATEGY_PATH,
                "report_dir": root / "funds",
                "orders_log": root / "orders.jsonl",
            }

            first_result = run_fund_nav_pipeline(input_path=first, **kwargs)
            self.assertEqual(first_result["new_orders"], 1)
            first_ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            self.assertEqual(first_ledger["positions"], {})
            order = first_ledger["pending_orders"][0]
            self.assertEqual(order["status"], "PENDING_NEXT_NAV")
            self.assertEqual(order["pricing_rule"], "NEXT_OPEN_DAY_CONFIRMED_NAV")

            second_result = run_fund_nav_pipeline(input_path=second, **kwargs)
            self.assertEqual(second_result["filled"], 1)
            second_ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            position = second_ledger["positions"]["110037"]
            self.assertGreater(position["quantity"], 0)
            self.assertEqual(position["acquired_date"], "2026-07-18")
            self.assertEqual(second_ledger["pending_orders"], [])
            event = json.loads(
                (root / "funds" / "2026-07-18-funds.json").read_text(encoding="utf-8")
            )["events"][0]
            self.assertEqual(event["fill_nav"], 1.116)
            self.assertGreater(event["fund_fee_cny"], 0)

    def test_open_and_nav_orders_share_cash_and_position_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fund_input = root / "fund.json"
            market_input = root / "market.json"
            fund_input.write_text(
                json.dumps(fund_snapshot("2026-07-17", 1.1157)), encoding="utf-8"
            )
            market_input.write_text(
                json.dumps(snapshot("2026-07-18", trading=True)), encoding="utf-8"
            )
            ledger_path = root / "portfolio.json"
            orders_log = root / "orders.jsonl"

            run_fund_nav_pipeline(
                input_path=fund_input,
                ledger_path=ledger_path,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "funds",
                orders_log=orders_log,
            )
            result = run_pipeline(
                input_path=market_input,
                ledger_path=ledger_path,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=orders_log,
            )

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            pending = ledger["pending_orders"]
            self.assertEqual(result["new_orders"], 1)
            self.assertEqual(
                {order["status"] for order in pending},
                {"PENDING_NEXT_NAV", "PENDING_NEXT_OPEN"},
            )
            reserved = sum(
                order.get("amount_cny", order.get("signal_close", 0) * order.get("quantity", 0))
                for order in pending
                if order["side"] == "BUY"
            )
            minimum_cash = 29900.0 * json.loads(
                STRATEGY_PATH.read_text(encoding="utf-8")
            )["risk"]["minimum_cash_weight"]
            self.assertLessEqual(reserved, 29900.0 - minimum_cash)

    def test_equity_orders_respect_total_weight_and_duplicate_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-17", trading=True)
            assets = []
            for index in range(4):
                asset = dict(value["assets"][0])
                asset["symbol"] = f"510{index:03d}"
                asset["name"] = f"ETF-{index}"
                asset["asset_type"] = "equity_etf"
                asset["history"] = [10.0 + point * 0.01 for point in range(20)]
                asset["close"] = asset["history"][-1]
                asset["source_ids"] = ["sse_etf", "eastmoney"]
                asset["risk_bucket"] = f"test_bucket_{index}"
                asset["exposure_group"] = "duplicate" if index > 1 else f"group_{index}"
                assets.append(asset)
            value["assets"] = assets
            input_path = root / "snapshot.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            strategy = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
            strategy["diversification"]["maximum_new_buys_per_cycle"] = 4
            strategy_path = root / "strategy.json"
            strategy_path.write_text(json.dumps(strategy), encoding="utf-8")

            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=strategy_path,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )

            decision = json.loads(
                (root / "reports" / "2026-07-17-short.json").read_text(encoding="utf-8")
            )
            planned = sum(order["signal_close"] * order["quantity"] for order in decision["new_orders"])
            self.assertLessEqual(planned, 29900.0 * strategy["risk"]["max_total_equity_weight"])
            self.assertEqual(result["new_orders"], 3)
            self.assertIn(
                "DUPLICATE_EXPOSURE_GROUP",
                decision["recommendations"][3]["reasons"],
            )


if __name__ == "__main__":
    unittest.main()
