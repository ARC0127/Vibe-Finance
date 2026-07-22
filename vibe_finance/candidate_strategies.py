from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import stdev
from typing import Any


CANDIDATE_SYMBOLS = ("510300", "510880", "512100", "518880")
GOLD_SYMBOL = "518880"
EQUITY_SYMBOLS = frozenset({"510300", "510880", "512100"})
MINIMUM_CLOSE_OBSERVATIONS = 274
VOLATILITY_RETURN_COUNT = 60
PROTOCOL_MANIFEST_ID = "b0-b1-b2-v1"
DEFAULT_CANDIDATE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "config/evaluation/b0-b1-b2-v1.json"
)
DEFAULT_B3_PROPOSAL = (
    Path(__file__).resolve().parents[1]
    / "config/evaluation/b3-mg-erc-proposal-v1.json"
)


@dataclass(frozen=True)
class CandidateProtocol:
    manifest_id: str
    manifest_sha256: str


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise CandidateSignalError(f"PROTOCOL_FIELD_MISSING:{'.'.join(keys)}")
        current = current[key]
    return current


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_candidate_protocol(path: Path) -> CandidateProtocol:
    """Bind the hard-coded v1 adapter semantics to an exact manifest file."""
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise CandidateSignalError(f"PROTOCOL_MANIFEST_UNREADABLE:{error}") from error
    expected: list[tuple[tuple[str, ...], Any]] = [
        (("manifest_id",), PROTOCOL_MANIFEST_ID),
        (("simulation_only",), True),
        (("dataset_contract", "required_prior_intervals_before_signal"), 273),
        (("dataset_contract", "required_close_observations_through_signal"), 274),
        (("candidates", "B1", "symbols"), list(CANDIDATE_SYMBOLS)),
        (("candidates", "B1", "momentum_windows_trading_days"), [126, 252]),
        (("candidates", "B1", "skip_recent_trading_days"), 21),
        (("candidates", "B1", "maximum_selected_assets"), 3),
        (("candidates", "B1", "risk_asset_budget"), 0.9),
        (("candidates", "B1", "no_trade_band_percentage_points"), 3.0),
        (("candidates", "B1", "minimum_order_notional_cny"), 2500.0),
        (("candidates", "B1", "rank_percentile_formula"), "average_one_based_ascending_rank_divided_by_eligible_count_with_symbol_ascending_final_tiebreak"),
        (("candidates", "B2", "symbols"), list(CANDIDATE_SYMBOLS)),
        (("candidates", "B2", "covariance_window_trading_days"), 60),
        (("candidates", "B2", "covariance_shrinkage", "sample_covariance_weight"), 0.75),
        (("candidates", "B2", "covariance_shrinkage", "diagonal_covariance_weight"), 0.25),
        (("candidates", "B2", "solver", "tolerance"), 1e-8),
        (("candidates", "B2", "solver", "long_only"), True),
        (("candidates", "B2", "solver", "maximum_iterations"), 1000),
        (("candidates", "B2", "solver", "minimum_variance_absolute"), 1e-15),
        (("candidates", "B2", "solver", "symmetry_absolute_tolerance"), 1e-12),
        (("candidates", "B2", "solver", "linear_pivot_absolute_tolerance"), 1e-18),
        (("candidates", "B2", "no_trade_band_percentage_points"), 3.0),
        (("candidates", "B2", "minimum_order_notional_cny"), 2500.0),
        (("shared_risk_limits", "maximum_single_etf_weight"), 0.2),
        (("shared_risk_limits", "maximum_gold_weight"), 0.1),
        (("shared_risk_limits", "maximum_total_equity_weight"), 0.7),
        (("shared_risk_limits", "minimum_cash_weight"), 0.1),
        (("execution_costs", "commission_rate"), 0.0003),
        (("execution_costs", "minimum_commission_cny"), 5.0),
        (("execution_costs", "base_one_way_slippage_bps"), 10),
        (("execution_costs", "stress_one_way_slippage_bps"), [25, 50]),
        (("execution_costs", "lot_size"), 100),
        (("execution_costs", "missing_or_untrusted_open_action"), "CANCEL"),
        (("execution_costs", "deferred_fill_allowed"), False),
    ]
    for keys, wanted in expected:
        actual = _nested(manifest, *keys)
        if actual != wanted:
            raise CandidateSignalError(
                f"PROTOCOL_SEMANTIC_MISMATCH:{'.'.join(keys)}:{actual!r}"
            )
    return CandidateProtocol(
        manifest_id=PROTOCOL_MANIFEST_ID,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


class CandidateSignalError(ValueError):
    """A candidate cannot produce a point-in-time signal without guessing."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _validated_common_prices(
    point_in_time_rows: list[dict[str, Any]],
    *,
    trading_calendar_dates: list[str],
    signal_date: str,
    minimum_observations: int,
) -> dict[str, list[float]]:
    if len(point_in_time_rows) < minimum_observations:
        raise CandidateSignalError(
            f"INSUFFICIENT_CLOSE_OBSERVATIONS:{len(point_in_time_rows)}"
        )
    expected_keys = {"date", *CANDIDATE_SYMBOLS}
    row_dates: list[str] = []
    validated = {symbol: [] for symbol in CANDIDATE_SYMBOLS}
    for row in point_in_time_rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise CandidateSignalError("COMMON_PANEL_SYMBOL_MISMATCH")
        raw_date = row.get("date")
        try:
            parsed_date = date.fromisoformat(str(raw_date))
        except ValueError as error:
            raise CandidateSignalError(f"COMMON_PANEL_DATE_INVALID:{raw_date}") from error
        if parsed_date.isoformat() != raw_date:
            raise CandidateSignalError(f"COMMON_PANEL_DATE_INVALID:{raw_date}")
        row_dates.append(raw_date)
        for symbol in CANDIDATE_SYMBOLS:
            converted = float(row[symbol])
            if not math.isfinite(converted) or converted <= 0.0:
                raise CandidateSignalError(f"INVALID_TOTAL_RETURN_CLOSE:{symbol}:{raw_date}")
            validated[symbol].append(converted)
    if row_dates != sorted(set(row_dates)):
        raise CandidateSignalError("COMMON_PANEL_DATES_NOT_STRICTLY_INCREASING")
    try:
        calendar = [date.fromisoformat(value).isoformat() for value in trading_calendar_dates]
    except ValueError as error:
        raise CandidateSignalError("TRADING_CALENDAR_DATE_INVALID") from error
    if calendar != sorted(set(calendar)):
        raise CandidateSignalError("TRADING_CALENDAR_NOT_STRICTLY_INCREASING")
    try:
        signal_index = calendar.index(signal_date)
    except ValueError as error:
        raise CandidateSignalError("SIGNAL_DATE_NOT_IN_TRADING_CALENDAR") from error
    start_index = signal_index - len(row_dates) + 1
    if start_index < 0 or row_dates != calendar[start_index : signal_index + 1]:
        raise CandidateSignalError("COMMON_PANEL_DATE_MISMATCH")
    if not row_dates or row_dates[-1] != signal_date:
        raise CandidateSignalError("SIGNAL_DATE_NOT_LAST_COMMON_TRADING_DATE")
    return validated


def _recent_simple_returns(values: list[float], count: int) -> list[float]:
    window = values[-(count + 1) :]
    return [window[index] / window[index - 1] - 1.0 for index in range(1, len(window))]


def _average_percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    result: dict[str, float] = {}
    index = 0
    count = len(ordered)
    while index < count:
        end = index + 1
        while end < count and ordered[end][1] == ordered[index][1]:
            end += 1
        average_one_based_rank = ((index + 1) + end) / 2.0
        percentile = average_one_based_rank / count
        for offset in range(index, end):
            result[ordered[offset][0]] = percentile
        index = end
    return result


def _asset_cap(symbol: str) -> float:
    return 0.1 if symbol == GOLD_SYMBOL else 0.2


def _load_b3_proposal(path: Path) -> str:
    try:
        raw = path.read_bytes()
        proposal = json.loads(raw)
        base_raw = DEFAULT_CANDIDATE_MANIFEST.read_bytes()
    except (OSError, json.JSONDecodeError) as error:
        raise CandidateSignalError(f"B3_PROPOSAL_UNREADABLE:{error}") from error
    expected = {
        "schema_version": 1,
        "proposal_id": "b3-momentum-gated-erc-v1",
        "status": "PREREGISTERED_PROPOSED_ONLY",
        "simulation_only": True,
        "base_manifest_id": PROTOCOL_MANIFEST_ID,
    }
    for key, value in expected.items():
        if proposal.get(key) != value:
            raise CandidateSignalError(f"B3_PROPOSAL_SEMANTIC_MISMATCH:{key}")
    if proposal.get("base_manifest_sha256") != hashlib.sha256(base_raw).hexdigest():
        raise CandidateSignalError("B3_PROPOSAL_BASE_MANIFEST_MISMATCH")
    candidate = proposal.get("candidate")
    exact_candidate_fields = {
        "id": "B3",
        "symbols": list(CANDIDATE_SYMBOLS),
        "signal_at": "last_trading_day_month_close",
        "execution_at": "next_trading_day_open",
        "new_numeric_hyperparameters": 0,
    }
    if not isinstance(candidate, dict) or any(
        candidate.get(key) != value for key, value in exact_candidate_fields.items()
    ):
        raise CandidateSignalError("B3_PROPOSAL_CANDIDATE_MISMATCH")
    eligibility = candidate.get("eligibility", {})
    allocation = candidate.get("allocation", {})
    if (
        eligibility.get("source") != "B1_absolute_momentum"
        or eligibility.get("windows_trading_days") != [126, 252]
        or eligibility.get("skip_recent_trading_days") != 21
        or allocation.get("source") != "B2_shrunk_covariance_erc"
        or allocation.get("covariance_window_trading_days") != 60
        or allocation.get("sample_covariance_weight") != 0.75
        or allocation.get("diagonal_covariance_weight") != 0.25
        or allocation.get("solver_tolerance") != 1e-8
        or allocation.get("solver_maximum_iterations") != 1000
    ):
        raise CandidateSignalError("B3_PROPOSAL_COMPONENT_SEMANTICS_MISMATCH")
    return hashlib.sha256(raw).hexdigest()


def _capped_redistribution(raw_proportions: dict[str, float]) -> dict[str, float]:
    weights = {symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}
    active = set(raw_proportions)
    remaining = 0.9
    while active and remaining > 1e-14:
        ordered_active = sorted(active)
        denominator = sum(raw_proportions[symbol] for symbol in ordered_active)
        if denominator <= 0.0:
            raise CandidateSignalError("INVALID_RISK_PROPORTIONS")
        proposed = {
            symbol: remaining * raw_proportions[symbol] / denominator
            for symbol in ordered_active
        }
        capped = [
            symbol
            for symbol in sorted(active)
            if weights[symbol] + proposed[symbol] > _asset_cap(symbol) + 1e-14
        ]
        if not capped:
            for symbol in ordered_active:
                weights[symbol] += proposed[symbol]
            remaining = 0.0
            break
        for symbol in capped:
            capacity = max(0.0, _asset_cap(symbol) - weights[symbol])
            weights[symbol] += capacity
            remaining -= capacity
            active.remove(symbol)

    equity_weight = sum(weights[symbol] for symbol in EQUITY_SYMBOLS)
    if equity_weight > 0.7:
        scale = 0.7 / equity_weight
        for symbol in EQUITY_SYMBOLS:
            weights[symbol] *= scale
    weights["CASH"] = 1.0 - sum(weights.values())
    return weights


def build_b1_target(
    point_in_time_rows: list[dict[str, Any]],
    *,
    trading_calendar_dates: list[str],
    signal_date: str,
) -> dict[str, Any]:
    """Build the preregistered monthly dual-momentum target at close t.

    The caller owns the monthly calendar rule. Only observations through the
    signal close may be passed. All four symbols must have a common complete
    panel; a partial panel fails closed instead of changing the universe.
    """
    protocol = load_candidate_protocol(DEFAULT_CANDIDATE_MANIFEST)
    prices = _validated_common_prices(
        point_in_time_rows,
        trading_calendar_dates=trading_calendar_dates,
        signal_date=signal_date,
        minimum_observations=MINIMUM_CLOSE_OBSERVATIONS,
    )
    signal_data_bindings = {
        "canonical_panel_sha256": _canonical_sha256(point_in_time_rows),
        "calendar_sha256": _canonical_sha256(trading_calendar_dates),
    }
    momentum_126: dict[str, float] = {}
    momentum_252: dict[str, float] = {}
    for symbol, values in prices.items():
        endpoint = values[-22]
        momentum_126[symbol] = endpoint / values[-148] - 1.0
        momentum_252[symbol] = endpoint / values[-274] - 1.0

    eligible = {
        symbol
        for symbol in CANDIDATE_SYMBOLS
        if momentum_126[symbol] > 0.0 and momentum_252[symbol] > 0.0
    }
    if not eligible:
        return {
            "status": "OK",
            "candidate": "B1",
            "signal_date": signal_date,
            "protocol_manifest_sha256": protocol.manifest_sha256,
            "data_bindings": signal_data_bindings,
            "target_weights": {**{symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}, "CASH": 1.0},
            "diagnostics": {
                "eligible_symbols": [],
                "selected_symbols": [],
                "momentum_126": momentum_126,
                "momentum_252": momentum_252,
            },
        }

    rank_126 = _average_percentile_ranks(
        {symbol: momentum_126[symbol] for symbol in eligible}
    )
    rank_252 = _average_percentile_ranks(
        {symbol: momentum_252[symbol] for symbol in eligible}
    )
    scores = {
        symbol: 0.5 * rank_126[symbol] + 0.5 * rank_252[symbol]
        for symbol in eligible
    }
    selected = sorted(eligible, key=lambda symbol: (-scores[symbol], symbol))[:3]
    annualized_volatility: dict[str, float] = {}
    for symbol in selected:
        returns = _recent_simple_returns(prices[symbol], VOLATILITY_RETURN_COUNT)
        volatility = stdev(returns) * math.sqrt(252.0)
        if not math.isfinite(volatility) or volatility <= 0.0:
            raise CandidateSignalError(f"INVALID_60D_VOLATILITY:{symbol}")
        annualized_volatility[symbol] = volatility
    inverse_total = sum(1.0 / annualized_volatility[symbol] for symbol in selected)
    raw_proportions = {
        symbol: (1.0 / annualized_volatility[symbol]) / inverse_total
        for symbol in selected
    }
    target = _capped_redistribution(raw_proportions)
    return {
        "status": "OK",
        "candidate": "B1",
        "signal_date": signal_date,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "data_bindings": signal_data_bindings,
        "target_weights": target,
        "diagnostics": {
            "eligible_symbols": sorted(eligible),
            "selected_symbols": selected,
            "momentum_126": momentum_126,
            "momentum_252": momentum_252,
            "rank_score": scores,
            "annualized_volatility": annualized_volatility,
        },
    }


def _sample_covariance(returns: list[list[float]]) -> list[list[float]]:
    row_count = len(returns)
    column_count = len(returns[0])
    means = [
        sum(row[column] for row in returns) / row_count
        for column in range(column_count)
    ]
    return [
        [
            sum(
                (row[left] - means[left]) * (row[right] - means[right])
                for row in returns
            )
            / (row_count - 1)
            for right in range(column_count)
        ]
        for left in range(column_count)
    ]


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(value * vector[index] for index, value in enumerate(row)) for row in matrix]


def _cholesky_positive_definite(matrix: list[list[float]]) -> None:
    size = len(matrix)
    lower = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(row + 1):
            value = matrix[row][column] - sum(
                lower[row][offset] * lower[column][offset]
                for offset in range(column)
            )
            if row == column:
                if not math.isfinite(value) or value <= 0.0:
                    raise CandidateSignalError("COVARIANCE_NOT_POSITIVE_DEFINITE")
                lower[row][column] = math.sqrt(value)
            else:
                lower[row][column] = value / lower[column][column]


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rhs)
    augmented = [matrix[row][:] + [rhs[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-18:
            raise CandidateSignalError("ERC_HESSIAN_SINGULAR")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        for offset in range(column, size + 1):
            augmented[column][offset] /= pivot_value
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            for offset in range(column, size + 1):
                augmented[row][offset] -= factor * augmented[column][offset]
    return [augmented[row][size] for row in range(size)]


def _erc_objective(covariance: list[list[float]], x: list[float]) -> float:
    marginal = _matvec(covariance, x)
    return 0.5 * sum(x[index] * marginal[index] for index in range(len(x))) - sum(
        (1.0 / len(x)) * math.log(value) for value in x
    )


def solve_equal_risk_contribution(
    covariance: list[list[float]], *, tolerance: float = 1e-8, maximum_iterations: int = 1000
) -> tuple[list[float], float, int]:
    size = len(covariance)
    if size == 0 or any(len(row) != size for row in covariance):
        raise CandidateSignalError("COVARIANCE_SHAPE_INVALID")
    if any(
        not math.isfinite(covariance[row][column])
        or abs(covariance[row][column] - covariance[column][row]) > 1e-12
        for row in range(size)
        for column in range(size)
    ):
        raise CandidateSignalError("COVARIANCE_INVALID_OR_ASYMMETRIC")
    if any(covariance[index][index] <= 1e-15 for index in range(size)):
        raise CandidateSignalError("COVARIANCE_VARIANCE_NONPOSITIVE")
    _cholesky_positive_definite(covariance)
    budgets = [1.0 / size] * size
    x = [1.0 / math.sqrt(size * covariance[index][index]) for index in range(size)]

    for iteration in range(maximum_iterations + 1):
        marginal = _matvec(covariance, x)
        residual = max(
            abs(x[index] * marginal[index] / budgets[index] - 1.0)
            for index in range(size)
        )
        if residual <= tolerance:
            total = sum(x)
            return [value / total for value in x], residual, iteration
        if iteration == maximum_iterations:
            break
        gradient = [
            marginal[index] - budgets[index] / x[index] for index in range(size)
        ]
        hessian = [row[:] for row in covariance]
        for index in range(size):
            hessian[index][index] += budgets[index] / (x[index] ** 2)
        direction = _solve_linear(hessian, [-value for value in gradient])
        directional_derivative = sum(
            gradient[index] * direction[index] for index in range(size)
        )
        if not math.isfinite(directional_derivative) or directional_derivative >= 0.0:
            raise CandidateSignalError("ERC_NON_DESCENT_DIRECTION")
        current_objective = _erc_objective(covariance, x)
        step = 1.0
        accepted = False
        for _ in range(80):
            candidate = [x[index] + step * direction[index] for index in range(size)]
            if all(value > 0.0 and math.isfinite(value) for value in candidate):
                objective = _erc_objective(covariance, candidate)
                if math.isfinite(objective) and objective <= (
                    current_objective + 1e-4 * step * directional_derivative
                ):
                    x = candidate
                    accepted = True
                    break
            step *= 0.5
        if not accepted:
            raise CandidateSignalError("ERC_LINE_SEARCH_FAILED")
    raise CandidateSignalError("ERC_MAXIMUM_ITERATIONS_EXCEEDED")


def _build_shrunk_erc_allocation(
    prices: dict[str, list[float]], symbols: tuple[str, ...]
) -> tuple[dict[str, float], dict[str, Any]]:
    if not symbols:
        raise CandidateSignalError("ERC_ELIGIBLE_SET_EMPTY")
    returns_by_symbol = {
        symbol: _recent_simple_returns(prices[symbol], VOLATILITY_RETURN_COUNT)
        for symbol in symbols
    }
    rows = [
        [returns_by_symbol[symbol][row] for symbol in symbols]
        for row in range(VOLATILITY_RETURN_COUNT)
    ]
    sample = _sample_covariance(rows)
    covariance = [
        [
            (sample[row][column] if row == column else 0.75 * sample[row][column])
            for column in range(len(symbols))
        ]
        for row in range(len(symbols))
    ]
    proportions, residual, iterations = solve_equal_risk_contribution(covariance)
    normalized_marginal = _matvec(covariance, proportions)
    normalized_variance = sum(
        proportions[index] * normalized_marginal[index]
        for index in range(len(proportions))
    )
    normalized_risk_budget_residual = max(
        abs(
            proportions[index] * normalized_marginal[index] / normalized_variance
            - 1.0 / len(proportions)
        )
        for index in range(len(proportions))
    )
    if normalized_risk_budget_residual > 1e-8:
        raise CandidateSignalError("ERC_NORMALIZED_RISK_BUDGET_RESIDUAL_EXCEEDED")
    cap_scales = [
        _asset_cap(symbol) / proportions[index]
        for index, symbol in enumerate(symbols)
    ]
    equity_proportion = sum(
        proportions[index]
        for index, symbol in enumerate(symbols)
        if symbol in EQUITY_SYMBOLS
    )
    scale_candidates = [0.9, *cap_scales]
    if equity_proportion > 0.0:
        scale_candidates.append(0.7 / equity_proportion)
    alpha = min(scale_candidates)
    weights = {symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}
    for index, symbol in enumerate(symbols):
        weights[symbol] = alpha * proportions[index]
    weights["CASH"] = 1.0 - sum(weights.values())
    diagnostics = {
        "sample_covariance": sample,
        "shrunk_covariance": covariance,
        "continuous_erc_proportions": dict(zip(symbols, proportions)),
        "continuous_erc_relative_residual": residual,
        "normalized_erc_risk_budget_residual": normalized_risk_budget_residual,
        "solver_iterations": iterations,
        "uniform_risk_scale": alpha,
    }
    return weights, diagnostics


def build_b2_target(
    point_in_time_rows: list[dict[str, Any]],
    *,
    trading_calendar_dates: list[str],
    signal_date: str,
) -> dict[str, Any]:
    """Build the preregistered monthly shrunk-covariance ERC target."""
    protocol = load_candidate_protocol(DEFAULT_CANDIDATE_MANIFEST)
    prices = _validated_common_prices(
        point_in_time_rows,
        trading_calendar_dates=trading_calendar_dates,
        signal_date=signal_date,
        minimum_observations=VOLATILITY_RETURN_COUNT + 1,
    )
    signal_data_bindings = {
        "canonical_panel_sha256": _canonical_sha256(point_in_time_rows),
        "calendar_sha256": _canonical_sha256(trading_calendar_dates),
    }
    weights, diagnostics = _build_shrunk_erc_allocation(
        prices, CANDIDATE_SYMBOLS
    )
    return {
        "status": "OK",
        "candidate": "B2",
        "signal_date": signal_date,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "data_bindings": signal_data_bindings,
        "target_weights": weights,
        "diagnostics": diagnostics,
    }


def build_b3_target(
    point_in_time_rows: list[dict[str, Any]],
    *,
    trading_calendar_dates: list[str],
    signal_date: str,
    proposal_path: Path = DEFAULT_B3_PROPOSAL,
) -> dict[str, Any]:
    """Build preregistered momentum-gated ERC without reading execution prices."""
    protocol = load_candidate_protocol(DEFAULT_CANDIDATE_MANIFEST)
    proposal_sha256 = _load_b3_proposal(proposal_path)
    prices = _validated_common_prices(
        point_in_time_rows,
        trading_calendar_dates=trading_calendar_dates,
        signal_date=signal_date,
        minimum_observations=MINIMUM_CLOSE_OBSERVATIONS,
    )
    momentum_126: dict[str, float] = {}
    momentum_252: dict[str, float] = {}
    for symbol, values in prices.items():
        endpoint = values[-22]
        momentum_126[symbol] = endpoint / values[-148] - 1.0
        momentum_252[symbol] = endpoint / values[-274] - 1.0
    eligible = tuple(
        symbol
        for symbol in CANDIDATE_SYMBOLS
        if momentum_126[symbol] > 0.0 and momentum_252[symbol] > 0.0
    )
    if eligible:
        weights, erc_diagnostics = _build_shrunk_erc_allocation(prices, eligible)
    else:
        weights = {symbol: 0.0 for symbol in CANDIDATE_SYMBOLS}
        weights["CASH"] = 1.0
        erc_diagnostics = None
    return {
        "status": "OK",
        "candidate": "B3",
        "signal_date": signal_date,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "candidate_proposal_sha256": proposal_sha256,
        "data_bindings": {
            "canonical_panel_sha256": _canonical_sha256(point_in_time_rows),
            "calendar_sha256": _canonical_sha256(trading_calendar_dates),
        },
        "target_weights": weights,
        "diagnostics": {
            "eligible_symbols": list(eligible),
            "momentum_126": momentum_126,
            "momentum_252": momentum_252,
            "erc": erc_diagnostics,
        },
        "claim_boundary": {
            "test_only_mechanical_candidate": True,
            "strategy_returns_computed": False,
            "promotion_authorized": False,
        },
    }
