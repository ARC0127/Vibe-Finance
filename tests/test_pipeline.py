from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_finance.pipeline import (
    DataGateError,
    _project_value,
    _trade_fees,
    _update_ledger_valuation,
    initialize_ledger,
    project_status,
    run_fund_nav_pipeline,
    run_pipeline,
    settle_open_orders,
    update_readme_status,
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
        "price_source_ids": ["source_a", "eastmoney"],
        "price_as_of": f"{run_date}T15:00:00+08:00",
    }
    if with_open:
        asset["price_as_of"] = f"{run_date}T09:30:00+08:00"
        asset["open"] = 100.02
        asset["open_source_ids"] = ["open_source_a", "open_source_b"]
        asset["open_price_as_of"] = f"{run_date}T09:35:00+08:00"
    return {
        "schema_version": 2,
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
            "price_source_ids": ["eastmoney", "efunds"],
            "price_as_of": f"{run_date}T22:30:00+08:00",
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


def cold_start_snapshot(
    run_date: str,
    *,
    market_state: str = "closed",
    as_of_time: str = "16:30:00",
    with_open: bool = False,
) -> dict:
    value = snapshot(run_date, trading=True)
    value["as_of"] = f"{run_date}T{as_of_time}+08:00"
    value["market_state"] = market_state
    assets = []
    fixtures = [
        ("518880", "黄金ETF", "gold_etf", 8.318, 0.0008, "gold", "gold"),
        ("159915", "创业板ETF", "equity_etf", 3.477, 0.004, "small_growth_equity", "chinext"),
        ("510300", "沪深300ETF", "equity_etf", 4.65, 0.0133, "core_equity", "csi300"),
        ("512100", "中证1000ETF", "equity_etf", 2.818, -0.0309, "small_growth_equity", "csi1000"),
    ]
    for symbol, name, asset_type, close, daily_return, bucket, group in fixtures:
        asset = {
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "close": close,
            "daily_return": daily_return,
            "lot_size": 100,
            "history": [],
            "source_ids": ["eastmoney", "exchange_price"],
            "price_source_ids": ["eastmoney", "exchange_price"],
            "price_as_of": value["as_of"],
            "primary_source_ids": ["exchange_price"],
            "risk_bucket": bucket,
            "exposure_group": group,
            "order_engine": "next_open",
        }
        if with_open:
            asset["open"] = close
            asset["open_source_ids"] = ["open_a", "open_b"]
            asset["open_price_as_of"] = f"{run_date}T09:35:00+08:00"
        assets.append(asset)
    value["assets"] = assets
    return value


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep historical fixtures deterministic. Production still evaluates
        # snapshot freshness against the real wall clock.
        self._datetime_patcher = patch("vibe_finance.pipeline.datetime", wraps=datetime)
        mocked_datetime = self._datetime_patcher.start()
        mocked_datetime.now.return_value = datetime(
            2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc
        )
        self.addCleanup(self._datetime_patcher.stop)

    def test_run_date_and_point_in_time_fields_fail_closed(self) -> None:
        strategy = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        timestamp_date = snapshot("2026-07-17", trading=True)
        timestamp_date["run_date"] = "2026-07-17T00:00:00"
        with self.assertRaisesRegex(DataGateError, "run_date"):
            validate_snapshot(timestamp_date, strategy)

        future_price = snapshot("2026-07-17", trading=True)
        future_price["assets"][0]["price_as_of"] = "2026-07-17T15:00:01+08:00"
        with self.assertRaisesRegex(DataGateError, "price_as_of"):
            validate_snapshot(future_price, strategy)

        future_evidence = snapshot("2026-07-17", trading=True)
        future_evidence["evidence"][0]["as_of"] = "2026-07-18"
        with self.assertRaisesRegex(DataGateError, "evidence"):
            validate_snapshot(future_evidence, strategy)

    def test_schema_v1_remains_readable_but_unscoped_prices_are_blocked(self) -> None:
        strategy = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        value = snapshot("2026-07-17", trading=True)
        value["schema_version"] = 1
        value["assets"][0].pop("price_as_of")
        value["assets"][0].pop("price_source_ids")
        warnings = validate_snapshot(value, strategy)
        self.assertIn("LEGACY_IMPLICIT_PRICE_AS_OF:511880", warnings)
        self.assertIn("LEGACY_UNSCOPED_PRICE_SOURCES:511880", warnings)

    def test_fractional_position_uses_one_mark_context_without_cost_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "portfolio.json"
            initialize_ledger(ledger_path)
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["positions"] = {
                "FUND": {
                    "quantity": "1.75",
                    "average_cost": 10.0,
                    "last_price": 20.0,
                    "last_price_as_of": "2026-07-16T22:30:00+08:00",
                }
            }
            values = _project_value(
                ledger, {}, "2026-07-17T22:30:00+08:00"
            )
            self.assertEqual(values["positions_cny"], 35.0)
            self.assertEqual(values["mark_status"], "STALE")
            self.assertEqual(
                values["blocks"], ["POSITION_MARK_NOT_IN_SNAPSHOT:FUND"]
            )
            _update_ledger_valuation(
                ledger, {}, values, "2026-07-17T22:30:00+08:00"
            )
            self.assertEqual(ledger["positions"]["FUND"]["market_value_cny"], 35.0)
            self.assertFalse(ledger["performance"]["valuation_complete"])
            self.assertIsNone(ledger["performance"]["last_valuation_as_of"])

            untrusted = _project_value(
                ledger,
                {"FUND": {"symbol": "FUND", "close": 999.0, "source_ids": ["legacy"]}},
                "2026-07-17T22:30:00+08:00",
            )
            self.assertEqual(untrusted["positions_cny"], 35.0)
            self.assertEqual(
                untrusted["blocks"], ["POSITION_MARK_UNTRUSTED:FUND"]
            )

            ledger["positions"]["FUND"].pop("last_price")
            ledger["positions"]["FUND"].pop("last_price_as_of")
            with self.assertRaisesRegex(DataGateError, "MISSING_POSITION_MARK"):
                _project_value(ledger, {}, "2026-07-17T22:30:00+08:00")

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
            value["assets"][0]["price_source_ids"] = ["source_a", "source_b"]
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
            asset["price_source_ids"] = ["eastmoney", "efunds"]
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
            self.assertGreater(Decimal(str(position["quantity"])), 0)
            self.assertEqual(position["acquired_date"], "2026-07-18")
            self.assertEqual(second_ledger["pending_orders"], [])
            event = json.loads(
                (root / "funds" / "2026-07-18-funds.json").read_text(encoding="utf-8")
            )["events"][0]
            self.assertEqual(event["fill_nav"], 1.116)
            self.assertGreater(event["fund_fee_cny"], 0)
            self.assertIsInstance(event["confirmed_shares"], str)
            self.assertIsInstance(position["quantity"], str)

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
                asset["price_source_ids"] = ["sse_etf", "eastmoney"]
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

    def test_cold_start_ranks_trend_and_controlled_dip_instead_of_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "close.json"
            input_path.write_text(
                json.dumps(cold_start_snapshot("2026-07-20")), encoding="utf-8"
            )
            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["new_orders"], 2)
            self.assertEqual(result["daily_execution_status"], "ORDER_SCHEDULED")
            ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            orders = ledger["pending_orders"]
            self.assertEqual({order["symbol"] for order in orders}, {"510300", "512100"})
            self.assertEqual(
                {order["signal_type"] for order in orders},
                {"COLD_START_TREND", "CONTROLLED_DIP"},
            )

    def test_preopen_signal_can_fill_later_the_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preopen_path = root / "preopen.json"
            open_path = root / "open.json"
            preopen_path.write_text(
                json.dumps(
                    cold_start_snapshot(
                        "2026-07-20", market_state="preopen", as_of_time="08:00:00"
                    )
                ),
                encoding="utf-8",
            )
            open_path.write_text(
                json.dumps(
                    cold_start_snapshot(
                        "2026-07-20",
                        market_state="open",
                        as_of_time="09:35:00",
                        with_open=True,
                    )
                ),
                encoding="utf-8",
            )
            ledger = root / "portfolio.json"
            run_pipeline(
                input_path=preopen_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "preopen",
                orders_log=root / "orders.jsonl",
                mode="preopen",
            )
            result = settle_open_orders(
                input_path=open_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "execution",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["filled"], 2)
            self.assertEqual(result["daily_execution_status"], "PASS")
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(value["performance"]["filled_trade_count"], 2)
            self.assertEqual(value["performance"]["last_filled_trade_date"], "2026-07-20")
            self.assertGreater(value["performance"]["cumulative_fees_cny"], 0)

    def test_unverified_preopen_status_creates_conditional_order_but_open_stays_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preopen = cold_start_snapshot(
                "2026-07-20", market_state="preopen", as_of_time="08:00:00"
            )
            for asset in preopen["assets"]:
                asset["trading_status"] = "UNVERIFIED_PREOPEN"
            preopen_path = root / "preopen.json"
            preopen_path.write_text(json.dumps(preopen), encoding="utf-8")
            ledger = root / "portfolio.json"
            result = run_pipeline(
                input_path=preopen_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "preopen",
                orders_log=root / "orders.jsonl",
                mode="preopen",
            )
            self.assertEqual(result["new_orders"], 2)
            self.assertEqual(result["daily_execution_status"], "ORDER_SCHEDULED")
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertTrue(
                all(
                    "PREOPEN_EXECUTION_STATUS_PENDING" in order["reasons"]
                    for order in value["pending_orders"]
                )
            )

            open_value = cold_start_snapshot(
                "2026-07-20",
                market_state="open",
                as_of_time="09:35:00",
                with_open=True,
            )
            for asset in open_value["assets"]:
                asset["trading_status"] = "SUSPENDED"
            open_path = root / "open.json"
            open_path.write_text(json.dumps(open_value), encoding="utf-8")
            settlement = settle_open_orders(
                input_path=open_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "execution",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(settlement["filled"], 0)
            self.assertEqual(settlement["status"], "FAILED_DAILY_TRADE")
            value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertTrue(
                all(
                    order["status"] == "CANCELLED_DATA_GATE"
                    for order in value["pending_orders"]
                )
            )

    def test_daily_fallback_creates_small_core_order_when_no_signal_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = cold_start_snapshot("2026-07-20")
            for asset in value["assets"]:
                asset["daily_return"] = 0.03
            core = next(asset for asset in value["assets"] if asset["symbol"] == "510300")
            core["daily_return"] = 0.0
            input_path = root / "close.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["new_orders"], 1)
            ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            order = ledger["pending_orders"][0]
            self.assertEqual(order["symbol"], "510300")
            self.assertEqual(order["signal_type"], "DAILY_EXPLORATION_FALLBACK")
            self.assertGreaterEqual(order["quantity"] * order["signal_close"], 800)

    def test_cash_and_bond_etfs_reach_candidate_and_order_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = snapshot("2026-07-20", trading=True)
            cash = value["assets"][0]
            cash.update(
                {
                    "risk_bucket": "cash_management",
                    "exposure_group": "cash_management",
                    "primary_source_ids": ["sse_etf", "yinhua"],
                    "trading_status": "TRADING",
                    "corporate_actions": [],
                    "history_adjusted_for_corporate_actions": True,
                }
            )
            bond_history = [10.0 + index * 0.001 for index in range(20)]
            bond = {
                "symbol": "511010",
                "name": "国债ETF国泰",
                "asset_type": "bond_etf",
                "close": bond_history[-1],
                "daily_return": 0.0001,
                "lot_size": 100,
                "history": bond_history,
                "source_ids": ["eastmoney", "sse_etf", "guotai_fund"],
                "price_source_ids": ["eastmoney", "sse_etf"],
                "price_as_of": "2026-07-20T15:00:00+08:00",
                "primary_source_ids": ["sse_etf", "guotai_fund"],
                "risk_bucket": "fixed_income",
                "exposure_group": "government_bond_5y",
                "order_engine": "next_open",
                "trading_status": "TRADING",
                "corporate_actions": [],
                "history_adjusted_for_corporate_actions": True,
            }
            value["assets"] = [cash, bond]
            input_path = root / "close.json"
            input_path.write_text(json.dumps(value), encoding="utf-8")
            result = run_pipeline(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["new_orders"], 2)
            ledger = json.loads((root / "portfolio.json").read_text(encoding="utf-8"))
            orders = {order["symbol"]: order for order in ledger["pending_orders"]}
            self.assertEqual(set(orders), {"511010", "511880"})
            self.assertTrue(all(order["status"] == "PENDING_NEXT_OPEN" for order in orders.values()))
            decision = json.loads((root / "reports" / "2026-07-20-short.json").read_text(encoding="utf-8"))
            signals = {item["symbol"]: item["signal_type"] for item in decision["recommendations"]}
            self.assertEqual(signals["511880"], "CASH_MANAGEMENT")
            self.assertEqual(signals["511010"], "FIXED_INCOME_TREND")

    def test_open_settlement_fails_daily_requirement_without_pending_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "open.json"
            input_path.write_text(
                json.dumps(
                    cold_start_snapshot(
                        "2026-07-20",
                        market_state="open",
                        as_of_time="09:35:00",
                        with_open=True,
                    )
                ),
                encoding="utf-8",
            )
            result = settle_open_orders(
                input_path=input_path,
                ledger_path=root / "portfolio.json",
                strategy_path=STRATEGY_PATH,
                report_dir=root / "execution",
                orders_log=root / "orders.jsonl",
            )
            self.assertEqual(result["status"], "FAILED_DAILY_TRADE")
            self.assertIn("NO_PENDING_DAILY_ORDER", result["blocks"])
            self.assertIn("DAILY_TRADE_REQUIREMENT_MISSED", result["blocks"])

    def test_readme_status_is_generated_from_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            initialize_ledger(ledger)
            readme = root / "README.md"
            readme.write_text(
                "# Demo\n\n<!-- VIBE_STATUS:START -->\nold\n<!-- VIBE_STATUS:END -->\n",
                encoding="utf-8",
            )
            result = update_readme_status(readme_path=readme, ledger_path=ledger)
            rendered = readme.read_text(encoding="utf-8")
            self.assertEqual(result["project_equity_cny"], 30000.0)
            self.assertIn("累计盈亏", rendered)
            self.assertIn("持平 +0.00 元", rendered)
            self.assertEqual(rendered.count("<!-- VIBE_STATUS:START -->"), 1)

    def test_readme_daily_strategy_is_generated_from_latest_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            initialize_ledger(ledger)
            inbox = root / "data" / "inbox"
            inbox.mkdir(parents=True)
            input_path = inbox / "2026-07-20.json"
            input_path.write_text(
                json.dumps(snapshot("2026-07-20", trading=True)),
                encoding="utf-8",
            )
            input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
            report_root = root / "reports"
            (report_root / "daily").mkdir(parents=True)
            (report_root / "execution").mkdir(parents=True)
            (report_root / "daily" / "2026-07-20-short.json").write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-20",
                        "as_of": "2026-07-20T16:30:00+08:00",
                        "mode": "short",
                        "input_sha256": input_hash,
                        "blocks": [],
                        "recommendations": [
                            {
                                "symbol": "511880",
                                "name": "银华日利ETF",
                                "action": "BUY",
                                "current_weight": 0.0,
                                "score": 0.4,
                                "reasons": ["CASH_MANAGEMENT_ELIGIBLE"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (report_root / "execution" / "2026-07-20-open.json").write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-20",
                        "as_of": "2026-07-20T09:35:00+08:00",
                        "mode": "open_settlement",
                        "events": [
                            {
                                "status": "CANCELLED_DATA_GATE",
                                "side": "BUY",
                                "symbol": "511880",
                                "quantity": 100,
                                "cancellation_reason": "OPEN_PRICE_NOT_CROSSCHECKED",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            readme = root / "README.md"
            readme.write_text(
                "# Demo\n\n"
                "<!-- VIBE_STATUS:START -->\nold ledger\n<!-- VIBE_STATUS:END -->\n\n"
                "<!-- VIBE_DAILY_STRATEGY:START -->\nold first-day strategy\n"
                "<!-- VIBE_DAILY_STRATEGY:END -->\n\n"
                "<!-- VIBE_DAILY_PLAN:START -->\nold five-day plan\n"
                "<!-- VIBE_DAILY_PLAN:END -->\n",
                encoding="utf-8",
            )

            result = update_readme_status(
                readme_path=readme,
                ledger_path=ledger,
                report_root=report_root,
                inbox_dir=inbox,
                strategy_path=STRATEGY_PATH,
            )
            rendered = readme.read_text(encoding="utf-8")

            self.assertTrue(result["daily_strategy_updated"])
            self.assertTrue(result["daily_plan_updated"])
            self.assertEqual(result["daily_strategy_run_date"], "2026-07-20")
            self.assertIn("每日策略日期：**2026-07-20**", rendered)
            self.assertIn("银华日利ETF", rendered)
            self.assertIn("现金管理条件通过", rendered)
            self.assertIn("OPEN_PRICE_NOT_CROSSCHECKED", rendered)
            self.assertIn("本交易日后续", rendered)
            self.assertNotIn("old first-day strategy", rendered)
            self.assertEqual(rendered.count("<!-- VIBE_DAILY_STRATEGY:START -->"), 1)

            first_render = rendered
            update_readme_status(
                readme_path=readme,
                ledger_path=ledger,
                report_root=report_root,
                inbox_dir=inbox,
                strategy_path=STRATEGY_PATH,
            )
            self.assertEqual(first_render, readme.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
