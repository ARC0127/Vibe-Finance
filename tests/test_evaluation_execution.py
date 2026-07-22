from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from vibe_finance.candidate_strategies import (
    CANDIDATE_SYMBOLS,
    build_b1_target,
    build_b2_target,
    build_b3_target,
    load_candidate_protocol,
)
from vibe_finance.evaluation_execution import (
    EvaluationExecutionError,
    canonical_sha256,
    plan_frozen_b0_orders,
    plan_rebalance_orders,
    simulate_next_open,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = load_candidate_protocol(
    ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json"
)
SOURCE_REGISTRY = {
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


def _targets(**overrides: float) -> dict[str, float]:
    result = {symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}
    result.update(overrides)
    result["CASH"] = 1.0 - sum(result.values())
    return result


def _open_observations(price: float = 10.0) -> dict[str, dict[str, object]]:
    return {
        symbol: {
            "trading_status": "TRADING",
            "corporate_actions": [],
            "observations": [
                {
                    "value": price,
                    "observed_at": "2026-01-06T09:31:00+08:00",
                    "source_id": source_id,
                    "raw_content_sha256": character * 64,
                }
                for source_id, character in (("source_a", "a"), ("source_b", "b"))
            ],
        }
        for symbol in CANDIDATE_SYMBOLS
    }


def _plan(
    *,
    target_weights: dict[str, float],
    current_quantities: dict[str, int] | None = None,
    signal_closes: dict[str, float] | None = None,
    nav_cny: float = 30000.0,
    available_to_sell_quantities: dict[str, int] | None = None,
    signal_date: str = "2026-01-05",
    execute_date: str = "2026-01-06",
    trading_calendar_dates: list[str] | None = None,
    source_registry: dict | None = None,
) -> dict:
    current = current_quantities or {}
    closes = signal_closes or {symbol: 10.0 for symbol in CANDIDATE_SYMBOLS}
    available = available_to_sell_quantities
    if available is None:
        available = dict(current)
    cash = nav_cny - sum(current.get(symbol, 0) * closes[symbol] for symbol in CANDIDATE_SYMBOLS)
    registry = source_registry or SOURCE_REGISTRY
    calendar = trading_calendar_dates or [signal_date, execute_date]
    bindings = {
        "readiness_artifact_sha256": "c" * 64,
        "canonical_panel_sha256": "d" * 64,
        "source_registry_sha256": canonical_sha256(registry),
        "calendar_sha256": canonical_sha256(calendar),
    }
    return plan_rebalance_orders(
        candidate_signal={
            "status": "OK",
            "candidate": "B1",
            "signal_date": signal_date,
            "protocol_manifest_sha256": PROTOCOL.manifest_sha256,
            "target_weights": target_weights,
            "data_bindings": {
                "canonical_panel_sha256": bindings["canonical_panel_sha256"],
                "calendar_sha256": bindings["calendar_sha256"],
            },
        },
        execute_date=execute_date,
        trading_calendar_dates=calendar,
        data_bindings=bindings,
        current_cash_cny=cash,
        current_quantities=current,
        available_to_sell_quantities=available,
        signal_closes=closes,
        nav_cny=nav_cny,
    )


def _execute(
    plan: dict,
    *,
    opens: dict | None = None,
    source_registry: dict | None = None,
    cash_cny: float | None = None,
    quantities: dict[str, int] | None = None,
    available: dict[str, int] | None = None,
) -> dict:
    pre = plan["pre_state"]
    return simulate_next_open(
        plan,
        open_observations=opens or _open_observations(),
        source_registry=source_registry or SOURCE_REGISTRY,
        execution_cutoff=f"{plan['execute_date']}T09:35:00+08:00",
        cash_cny=pre["cash_cny"] if cash_cny is None else cash_cny,
        current_quantities=pre["quantities"] if quantities is None else quantities,
        available_to_sell_quantities=(
            pre["available_to_sell_quantities"] if available is None else available
        ),
    )


def _b0_plan() -> dict:
    calendar = ["2026-01-05", "2026-01-06"]
    bindings = {
        "readiness_artifact_sha256": "c" * 64,
        "canonical_panel_sha256": "d" * 64,
        "source_registry_sha256": canonical_sha256(SOURCE_REGISTRY),
        "calendar_sha256": canonical_sha256(calendar),
    }
    signal = {
        "status": "OK",
        "candidate": "B0",
        "signal_date": "2026-01-05",
        "protocol_manifest_sha256": PROTOCOL.manifest_sha256,
        "frozen_source_contract_sha256": "e" * 64,
        "data_bindings": {
            "canonical_panel_sha256": bindings["canonical_panel_sha256"],
            "calendar_sha256": bindings["calendar_sha256"],
        },
        "orders": [
            {
                "symbol": "510300",
                "side": "BUY",
                "quantity": 600,
                "signal_close": 9.9,
                "raw_open_limit": 10.0485,
                "signal_type": "TREND",
                "score": 1.26,
            },
            {
                "symbol": "512100",
                "side": "BUY",
                "quantity": 200,
                "signal_close": 9.95,
                "raw_open_limit": 10.09925,
                "signal_type": "CONTROLLED_DIP",
                "score": 1.25,
            },
        ],
    }
    signal["signal_sha256"] = canonical_sha256(signal)
    return plan_frozen_b0_orders(
        frozen_signal=signal,
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


class EvaluationExecutionTests(unittest.TestCase):
    def test_frozen_b0_fixed_quantities_use_common_costs_without_b1_b2_gates(self) -> None:
        plan = _b0_plan()
        self.assertEqual(
            plan["execution_semantics"],
            "FROZEN_FIXED_QUANTITY_RAW_OPEN_LIMIT",
        )
        self.assertEqual(
            [(order["symbol"], order["quantity"]) for order in plan["orders"]],
            [("510300", 600), ("512100", 200)],
        )
        result = _execute(plan)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual([fill["fill_price"] for fill in result["fills"]], [10.01, 10.01])
        self.assertEqual(result["commission_cny"], 10.0)
        self.assertEqual(result["slippage_cny"], 8.0)
        self.assertEqual(result["after_state"]["cash_cny"], 21882.0)
        self.assertEqual(result["after_state"]["quantities"]["510300"], 600)
        self.assertEqual(result["after_state"]["quantities"]["512100"], 200)

    def test_frozen_b0_raw_open_limit_is_checked_before_common_slippage(self) -> None:
        plan = _b0_plan()
        opens = _open_observations()
        for observation in opens["510300"]["observations"]:
            observation["value"] = 10.05
        result = _execute(plan, opens=opens)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(
            [(item["symbol"], item["status"]) for item in result["cancellations"]],
            [("510300", "CANCELLED_RAW_OPEN_LIMIT")],
        )
        self.assertEqual([fill["symbol"] for fill in result["fills"]], ["512100"])

    def test_plan_is_independent_of_next_open_and_executes_next_day(self) -> None:
        arguments = {
            "target_weights": _targets(
                **{"510300": 0.2, "510880": 0.2, "512100": 0.2, "518880": 0.1}
            ),
            "current_quantities": {},
            "signal_closes": {symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            "nav_cny": 30000.0,
        }
        plan = _plan(**arguments)
        self.assertFalse(plan["claim_boundary"]["next_open_observed"])
        self.assertEqual([order["quantity"] for order in plan["orders"]], [600, 600, 600, 300])
        result = _execute(plan)
        self.assertEqual(len(result["fills"]), 4)
        self.assertEqual(result["commission_cny"], 20.0)
        self.assertEqual(result["slippage_cny"], 21.0)
        self.assertTrue(result["claim_boundary"]["simulation_only"])
        self.assertFalse(result["claim_boundary"]["performance_claim_authorized"])

    def test_band_and_notional_must_both_pass(self) -> None:
        common = {
            "signal_date": "2026-01-05",
            "execute_date": "2026-01-06",
            "current_quantities": {"510300": 100},
            "signal_closes": {symbol: 100.0 for symbol in CANDIDATE_SYMBOLS},
        }
        band_fail = _plan(
            current_quantities=common["current_quantities"],
            signal_closes=common["signal_closes"],
            target_weights=_targets(**{"510300": 0.1299}),
            nav_cny=100000.0,
        )
        self.assertEqual(band_fail["orders"], [])
        notional_fail = _plan(
            current_quantities={"510300": 50},
            signal_closes={symbol: 100.0 for symbol in CANDIDATE_SYMBOLS},
            target_weights=_targets(**{"510300": 0.13}),
            nav_cny=50000.0,
        )
        self.assertEqual(notional_fail["orders"], [])
        exact_gate = _plan(
            current_quantities={"510300": 1000},
            signal_closes={symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            target_weights=_targets(**{"510300": 0.13}),
            nav_cny=100000.0,
        )
        self.assertEqual(len(exact_gate["orders"]), 1)
        self.assertEqual(exact_gate["orders"][0]["quantity"], 300)
        rounded_below_minimum = _plan(
            target_weights=_targets(**{"510300": 0.03}),
            signal_closes={symbol: 24.0 for symbol in CANDIDATE_SYMBOLS},
            nav_cny=100000.0,
        )
        self.assertEqual(rounded_below_minimum["orders"], [])
        self.assertEqual(
            rounded_below_minimum["skipped"][0]["reason"],
            "MINIMUM_NOTIONAL_FAILED_AFTER_LOT_ROUNDING",
        )

    def test_full_exit_bypasses_ordinary_order_gate(self) -> None:
        plan = _plan(
            target_weights=_targets(),
            current_quantities={"510300": 100},
            signal_closes={symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            nav_cny=1_000_000.0,
        )
        self.assertEqual(len(plan["orders"]), 1)
        self.assertEqual(plan["orders"][0]["gate_bypass"], "FULL_EXIT")
        self.assertEqual(plan["orders"][0]["quantity"], 100)

    def test_untrusted_open_cancels_without_deferred_fill(self) -> None:
        plan = _plan(
            target_weights=_targets(**{"510300": 0.2}),
            current_quantities={},
            signal_closes={symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            nav_cny=30000.0,
        )
        opens = _open_observations()
        opens["510300"]["observations"][1]["source_id"] = "source_a"
        result = _execute(plan, opens=opens)
        self.assertEqual(result["fills"], [])
        self.assertEqual(len(result["cancellations"]), 1)
        self.assertIn("OPEN_SOURCE_INDEPENDENCE_INSUFFICIENT", result["cancellations"][0]["reason"])
        self.assertEqual(result["status"], "ALL_CANCELLED")
        self.assertEqual(result["trusted_open_count"], 0)

    def test_plan_requires_exact_next_bound_trading_date(self) -> None:
        with self.assertRaisesRegex(
            EvaluationExecutionError, "next bound trading date"
        ):
            _plan(
                target_weights=_targets(**{"510300": 0.2}),
                execute_date="2026-01-07",
                trading_calendar_dates=["2026-01-05", "2026-01-06", "2026-01-07"],
            )

    def test_execution_rejects_pre_state_or_source_registry_substitution(self) -> None:
        plan = _plan(target_weights=_targets(**{"510300": 0.2}))
        tampered_plan = copy.deepcopy(plan)
        tampered_plan["orders"][0]["quantity"] += 100
        with self.assertRaisesRegex(EvaluationExecutionError, "PLAN_HASH_MISMATCH"):
            _execute(tampered_plan)
        with self.assertRaisesRegex(
            EvaluationExecutionError, "EXECUTION_PRE_STATE_BINDING_MISMATCH"
        ):
            _execute(plan, cash_cny=29999.0)
        substituted = copy.deepcopy(SOURCE_REGISTRY)
        substituted["sources"][0]["independence_group"] = "changed"
        with self.assertRaisesRegex(
            EvaluationExecutionError, "SOURCE_REGISTRY_BINDING_MISMATCH"
        ):
            _execute(plan, source_registry=substituted)

    def test_sells_are_planned_before_buys(self) -> None:
        plan = _plan(
            target_weights=_targets(**{"510880": 0.2}),
            current_quantities={"510300": 1000},
            signal_closes={symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            nav_cny=50000.0,
        )
        self.assertEqual([order["side"] for order in plan["orders"]], ["SELL", "BUY"])

    def test_planning_output_does_not_change_when_future_open_changes(self) -> None:
        arguments = {
            "signal_date": "2026-01-05",
            "execute_date": "2026-01-06",
            "target_weights": _targets(**{"510300": 0.2}),
            "current_quantities": {},
            "signal_closes": {symbol: 10.0 for symbol in CANDIDATE_SYMBOLS},
            "nav_cny": 30000.0,
        }
        first = _plan(**copy.deepcopy(arguments))
        second = _plan(**copy.deepcopy(arguments))
        self.assertEqual(first, second)

    def test_b1_b2_b3_synthetic_signal_execution_save_reload_round_trip(self) -> None:
        prices = {
            symbol: [
                10.0
                * math.exp(
                    (0.0004 + index * 0.00007) * day
                    + 0.003 * math.sin(day * (0.19 + index * 0.017))
                )
                for day in range(300)
            ]
            for index, symbol in enumerate(CANDIDATE_SYMBOLS)
        }
        artifact = {
            "schema_version": 1,
            "evidence_class": "TEST_SYNTHETIC_MECHANICAL_ONLY",
            "B0": {
                "status": "NOT_RUN",
                "reason": "EXACT_FROZEN_ADAPTER_NOT_IMPLEMENTED",
                "metrics": None,
            },
            "candidates": {},
            "strategy_ranking": None,
            "promotion_authorized": False,
        }
        dates = [
            (date(2025, 3, 12) + timedelta(days=index)).isoformat()
            for index in range(300)
        ]
        rows = [
            {"date": dates[index], **{symbol: prices[symbol][index] for symbol in CANDIDATE_SYMBOLS}}
            for index in range(300)
        ]
        for candidate, signal in (
            (
                "B1",
                build_b1_target(
                    rows,
                    trading_calendar_dates=dates + ["2026-01-06"],
                    signal_date=dates[-1],
                ),
            ),
            (
                "B2",
                build_b2_target(
                    rows,
                    trading_calendar_dates=dates + ["2026-01-06"],
                    signal_date=dates[-1],
                ),
            ),
            (
                "B3",
                build_b3_target(
                    rows,
                    trading_calendar_dates=dates + ["2026-01-06"],
                    signal_date=dates[-1],
                ),
            ),
        ):
            plan = plan_rebalance_orders(
                candidate_signal=signal,
                execute_date="2026-01-06",
                trading_calendar_dates=dates + ["2026-01-06"],
                data_bindings={
                    "readiness_artifact_sha256": "c" * 64,
                    "canonical_panel_sha256": signal["data_bindings"]["canonical_panel_sha256"],
                    "source_registry_sha256": canonical_sha256(SOURCE_REGISTRY),
                    "calendar_sha256": signal["data_bindings"]["calendar_sha256"],
                },
                current_cash_cny=29900.0,
                current_quantities={},
                available_to_sell_quantities={},
                signal_closes={symbol: prices[symbol][-1] for symbol in CANDIDATE_SYMBOLS},
                nav_cny=29900.0,
            )
            opens = _open_observations()
            for symbol in CANDIDATE_SYMBOLS:
                for source_observation in opens[symbol]["observations"]:
                    source_observation["value"] = prices[symbol][-1] * 1.001
            execution = simulate_next_open(
                plan,
                open_observations=opens,
                source_registry=SOURCE_REGISTRY,
                execution_cutoff="2026-01-06T09:35:00+08:00",
                cash_cny=29900.0,
                current_quantities={},
                available_to_sell_quantities={},
            )
            artifact["candidates"][candidate] = {
                "signal": signal,
                "plan": plan,
                "execution": execution,
                "metrics": None,
            }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-mechanical.json"
            path.write_text(
                json.dumps(artifact, ensure_ascii=False, sort_keys=True, allow_nan=False),
                encoding="utf-8",
            )
            reloaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded, artifact)
        self.assertIsNone(reloaded["strategy_ranking"])
        self.assertFalse(reloaded["promotion_authorized"])
        self.assertIsNone(reloaded["candidates"]["B1"]["metrics"])
        self.assertIsNone(reloaded["candidates"]["B2"]["metrics"])
        self.assertIsNone(reloaded["candidates"]["B3"]["metrics"])
        self.assertEqual(
            reloaded["candidates"]["B3"]["plan"]["candidate_proposal_sha256"],
            reloaded["candidates"]["B3"]["signal"]["candidate_proposal_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
