from __future__ import annotations

import math
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from vibe_finance.candidate_strategies import (
    CANDIDATE_SYMBOLS,
    CandidateSignalError,
    build_b1_target,
    build_b2_target,
    build_b3_target,
    load_candidate_protocol,
    solve_equal_risk_contribution,
)


ROOT = Path(__file__).resolve().parents[1]


def _synthetic_prices(length: int = 300) -> dict[str, list[float]]:
    return {
        symbol: [
            100.0
            * math.exp(
                (0.00045 + index * 0.00008) * day
                + 0.004 * math.sin(day * (0.17 + index * 0.013))
            )
            for day in range(length)
        ]
        for index, symbol in enumerate(CANDIDATE_SYMBOLS)
    }


def _dated_panel(prices: dict[str, list[float]]) -> tuple[list[dict[str, float | str]], list[str]]:
    length = len(next(iter(prices.values())))
    dates = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(length)]
    rows = [
        {"date": dates[index], **{symbol: prices[symbol][index] for symbol in prices}}
        for index in range(length)
    ]
    return rows, dates


def _b1(prices: dict[str, list[float]]) -> dict:
    rows, dates = _dated_panel(prices)
    return build_b1_target(
        rows,
        trading_calendar_dates=dates,
        signal_date=dates[-1],
    )


def _b2(prices: dict[str, list[float]]) -> dict:
    rows, dates = _dated_panel(prices)
    return build_b2_target(
        rows,
        trading_calendar_dates=dates,
        signal_date=dates[-1],
    )


def _b3(prices: dict[str, list[float]], proposal_path: Path | None = None) -> dict:
    rows, dates = _dated_panel(prices)
    arguments = {}
    if proposal_path is not None:
        arguments["proposal_path"] = proposal_path
    return build_b3_target(
        rows,
        trading_calendar_dates=dates,
        signal_date=dates[-1],
        **arguments,
    )


class CandidateStrategyTests(unittest.TestCase):
    def test_b3_all_eligible_is_exactly_b2(self) -> None:
        prices = _synthetic_prices()
        b2 = _b2(prices)
        b3 = _b3(prices)
        self.assertEqual(b3["diagnostics"]["eligible_symbols"], list(CANDIDATE_SYMBOLS))
        self.assertEqual(b3["target_weights"], b2["target_weights"])
        self.assertEqual(b3["diagnostics"]["erc"], b2["diagnostics"])
        self.assertFalse(b3["claim_boundary"]["strategy_returns_computed"])

    def test_b3_all_ineligible_is_cash_and_single_eligible_hits_cap(self) -> None:
        falling = {
            symbol: [
                100.0
                * math.exp(-0.001 * day + 0.003 * math.sin(day * (0.17 + index * 0.01)))
                for day in range(300)
            ]
            for index, symbol in enumerate(CANDIDATE_SYMBOLS)
        }
        cash = _b3(falling)
        self.assertEqual(cash["diagnostics"]["eligible_symbols"], [])
        self.assertEqual(
            cash["target_weights"],
            {**{symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}, "CASH": 1.0},
        )

        one = {symbol: values[:] for symbol, values in falling.items()}
        one["510300"] = _synthetic_prices()["510300"]
        single = _b3(one)
        self.assertEqual(single["diagnostics"]["eligible_symbols"], ["510300"])
        self.assertAlmostEqual(single["target_weights"]["510300"], 0.2)
        self.assertAlmostEqual(single["target_weights"]["CASH"], 0.8)
        self.assertLessEqual(
            single["diagnostics"]["erc"]["normalized_erc_risk_budget_residual"],
            1e-8,
        )

    def test_b3_subset_erc_caps_and_proposal_drift_fail_closed(self) -> None:
        rising = _synthetic_prices()
        falling = {
            symbol: [100.0 * math.exp(-0.001 * day + 0.002 * math.sin(day * 0.19)) for day in range(300)]
            for symbol in CANDIDATE_SYMBOLS
        }
        mixed = {symbol: falling[symbol][:] for symbol in CANDIDATE_SYMBOLS}
        mixed["510300"] = rising["510300"]
        mixed["518880"] = rising["518880"]
        result = _b3(mixed)
        self.assertEqual(result["diagnostics"]["eligible_symbols"], ["510300", "518880"])
        self.assertLessEqual(result["target_weights"]["510300"], 0.2 + 1e-12)
        self.assertLessEqual(result["target_weights"]["518880"], 0.1 + 1e-12)
        self.assertGreaterEqual(result["target_weights"]["CASH"], 0.1 - 1e-12)
        self.assertLessEqual(
            result["diagnostics"]["erc"]["normalized_erc_risk_budget_residual"],
            1e-8,
        )

        proposal = json.loads(
            (ROOT / "config" / "evaluation" / "b3-mg-erc-proposal-v1.json").read_text(
                encoding="utf-8"
            )
        )
        proposal["candidate"]["allocation"]["solver_tolerance"] = 1e-7
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            path.write_text(json.dumps(proposal), encoding="utf-8")
            with self.assertRaisesRegex(
                CandidateSignalError, "B3_PROPOSAL_COMPONENT_SEMANTICS_MISMATCH"
            ):
                _b3(mixed, path)

    def test_b1_requires_274_close_observations(self) -> None:
        with self.assertRaisesRegex(
            CandidateSignalError, "INSUFFICIENT_CLOSE_OBSERVATIONS"
        ):
            _b1(_synthetic_prices(273))
        self.assertEqual(_b1(_synthetic_prices(274))["status"], "OK")

    def test_b1_skip_window_does_not_change_momentum(self) -> None:
        original = _synthetic_prices()
        changed = {symbol: values[:] for symbol, values in original.items()}
        for symbol in CANDIDATE_SYMBOLS:
            for offset in range(1, 22):
                changed[symbol][-offset] *= 1.0 + offset * 0.0002
        before = _b1(original)
        after = _b1(changed)
        self.assertEqual(
            before["diagnostics"]["momentum_126"],
            after["diagnostics"]["momentum_126"],
        )
        self.assertEqual(
            before["diagnostics"]["momentum_252"],
            after["diagnostics"]["momentum_252"],
        )
        self.assertNotEqual(
            before["diagnostics"]["annualized_volatility"],
            after["diagnostics"]["annualized_volatility"],
        )

    def test_b1_ties_are_deterministic_and_caps_hold(self) -> None:
        base = _synthetic_prices()["510300"]
        result = _b1({symbol: base[:] for symbol in CANDIDATE_SYMBOLS})
        self.assertEqual(
            result["diagnostics"]["selected_symbols"],
            ["510300", "510880", "512100"],
        )
        weights = result["target_weights"]
        self.assertLessEqual(weights["510300"], 0.2)
        self.assertLessEqual(weights["510880"], 0.2)
        self.assertLessEqual(weights["512100"], 0.2)
        self.assertLessEqual(weights["518880"], 0.1)
        self.assertLessEqual(sum(weights[symbol] for symbol in ("510300", "510880", "512100")), 0.7)
        self.assertGreaterEqual(weights["CASH"], 0.1)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_b1_common_panel_failure_does_not_change_universe(self) -> None:
        prices = _synthetic_prices()
        del prices["518880"]
        with self.assertRaisesRegex(CandidateSignalError, "COMMON_PANEL_SYMBOL_MISMATCH"):
            _b1(prices)

    def test_b1_rejects_internal_common_calendar_gap(self) -> None:
        rows, dates = _dated_panel(_synthetic_prices())
        del rows[100]
        with self.assertRaisesRegex(CandidateSignalError, "COMMON_PANEL_DATE_MISMATCH"):
            build_b1_target(
                rows,
                trading_calendar_dates=dates,
                signal_date=dates[-1],
            )

    def test_candidate_adapter_rejects_manifest_semantic_drift(self) -> None:
        manifest = json.loads(
            (ROOT / "config" / "evaluation" / "b0-b1-b2-v1.json").read_text(
                encoding="utf-8"
            )
        )
        manifest["candidates"]["B1"]["maximum_selected_assets"] = 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                CandidateSignalError, "PROTOCOL_SEMANTIC_MISMATCH"
            ):
                load_candidate_protocol(path)

    def test_erc_diagonal_covariance_is_inverse_volatility(self) -> None:
        variances = [1.0, 4.0, 9.0, 16.0]
        covariance = [
            [variance if row == column else 0.0 for column in range(4)]
            for row, variance in enumerate(variances)
        ]
        weights, residual, _ = solve_equal_risk_contribution(covariance)
        expected_raw = [1.0, 0.5, 1.0 / 3.0, 0.25]
        expected = [value / sum(expected_raw) for value in expected_raw]
        for actual, wanted in zip(weights, expected):
            self.assertAlmostEqual(actual, wanted, places=10)
        self.assertLessEqual(residual, 1e-8)

    def test_b2_shrinkage_and_uniform_cap_scaling(self) -> None:
        result = _b2(_synthetic_prices())
        diagnostics = result["diagnostics"]
        sample = diagnostics["sample_covariance"]
        shrunk = diagnostics["shrunk_covariance"]
        for row in range(4):
            for column in range(4):
                multiplier = 1.0 if row == column else 0.75
                self.assertAlmostEqual(shrunk[row][column], sample[row][column] * multiplier)
        weights = result["target_weights"]
        proportions = diagnostics["continuous_erc_proportions"]
        alpha = diagnostics["uniform_risk_scale"]
        for symbol in CANDIDATE_SYMBOLS:
            self.assertAlmostEqual(weights[symbol], proportions[symbol] * alpha)
        self.assertLessEqual(weights["518880"], 0.1 + 1e-12)
        self.assertGreaterEqual(weights["CASH"], 0.1)
        self.assertLessEqual(diagnostics["normalized_erc_risk_budget_residual"], 1e-8)

    def test_erc_fail_closed_on_zero_variance_or_nonconvergence(self) -> None:
        with self.assertRaisesRegex(
            CandidateSignalError, "COVARIANCE_VARIANCE_NONPOSITIVE"
        ):
            solve_equal_risk_contribution(
                [[0.0 if row == column == 0 else (1.0 if row == column else 0.0) for column in range(4)] for row in range(4)]
            )
        covariance = [
            [2.0 if row == column else 0.2 for column in range(4)]
            for row in range(4)
        ]
        with self.assertRaisesRegex(
            CandidateSignalError, "ERC_MAXIMUM_ITERATIONS_EXCEEDED"
        ):
            solve_equal_risk_contribution(
                covariance, tolerance=1e-30, maximum_iterations=0
            )


if __name__ == "__main__":
    unittest.main()
