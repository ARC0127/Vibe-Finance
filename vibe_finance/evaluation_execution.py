from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from .candidate_strategies import (
    CANDIDATE_SYMBOLS,
    EQUITY_SYMBOLS,
    GOLD_SYMBOL,
    DEFAULT_CANDIDATE_MANIFEST,
    load_candidate_protocol,
)


LOT_SIZE = 100
WEIGHT_BAND = Decimal("0.03")
MINIMUM_ORDER_NOTIONAL = Decimal("2500")
COMMISSION_RATE = Decimal("0.0003")
MINIMUM_COMMISSION = Decimal("5")


class EvaluationExecutionError(ValueError):
    """The common simulated execution contract cannot be applied safely."""


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _decimal(value: Any, label: str) -> Decimal:
    try:
        converted = Decimal(str(value))
    except Exception as error:
        raise EvaluationExecutionError(f"{label} is not numeric") from error
    if not converted.is_finite():
        raise EvaluationExecutionError(f"{label} is not finite")
    return converted


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _asset_cap(symbol: str) -> Decimal:
    return Decimal("0.1") if symbol == GOLD_SYMBOL else Decimal("0.2")


def _normalize_position_accounting(
    position_accounting: dict[str, dict[str, Any]] | None,
    quantities: dict[str, int],
) -> dict[str, dict[str, Any]]:
    raw = position_accounting or {}
    expected = {symbol for symbol, quantity in quantities.items() if quantity > 0}
    if set(raw) != expected:
        raise EvaluationExecutionError("POSITION_ACCOUNTING_SYMBOL_MISMATCH")
    result: dict[str, dict[str, Any]] = {}
    for symbol in sorted(expected):
        item = raw.get(symbol)
        if not isinstance(item, dict):
            raise EvaluationExecutionError(f"POSITION_ACCOUNTING_INVALID:{symbol}")
        average_cost = _decimal(item.get("average_cost"), f"average cost {symbol}")
        acquired_date = str(item.get("acquired_date", ""))
        last_buy_date = str(item.get("last_buy_date", ""))
        try:
            date.fromisoformat(acquired_date)
            date.fromisoformat(last_buy_date)
        except ValueError as error:
            raise EvaluationExecutionError(
                f"POSITION_ACCOUNTING_DATE_INVALID:{symbol}"
            ) from error
        if average_cost <= 0 or last_buy_date < acquired_date:
            raise EvaluationExecutionError(f"POSITION_ACCOUNTING_INVALID:{symbol}")
        result[symbol] = {
            "quantity": quantities[symbol],
            "average_cost": float(_price(average_cost)),
            "acquired_date": acquired_date,
            "last_buy_date": last_buy_date,
        }
    return result


def plan_rebalance_orders(
    *,
    candidate_signal: dict[str, Any],
    execute_date: str,
    trading_calendar_dates: list[str],
    data_bindings: dict[str, str],
    current_cash_cny: float,
    current_quantities: dict[str, int],
    available_to_sell_quantities: dict[str, int],
    signal_closes: dict[str, float],
    nav_cny: float,
) -> dict[str, Any]:
    """Create t-close quantities without observing the t+1 open."""
    protocol = load_candidate_protocol(DEFAULT_CANDIDATE_MANIFEST)
    signal_date = str(candidate_signal.get("signal_date", ""))
    if (
        candidate_signal.get("status") != "OK"
        or candidate_signal.get("protocol_manifest_sha256") != protocol.manifest_sha256
    ):
        raise EvaluationExecutionError("CANDIDATE_SIGNAL_PROTOCOL_MISMATCH")
    candidate = candidate_signal.get("candidate")
    candidate_proposal_sha256 = candidate_signal.get("candidate_proposal_sha256")
    if candidate == "B3":
        if not _is_sha256(candidate_proposal_sha256):
            raise EvaluationExecutionError("B3_PROPOSAL_BINDING_MISSING")
    elif candidate_proposal_sha256 is not None:
        raise EvaluationExecutionError("UNEXPECTED_CANDIDATE_PROPOSAL_BINDING")
    target_weights = candidate_signal.get("target_weights")
    if not isinstance(target_weights, dict):
        raise EvaluationExecutionError("candidate signal has no target_weights")
    required_bindings = {
        "readiness_artifact_sha256",
        "canonical_panel_sha256",
        "source_registry_sha256",
        "calendar_sha256",
    }
    if set(data_bindings) != required_bindings or any(
        not _is_sha256(value) for value in data_bindings.values()
    ):
        raise EvaluationExecutionError("DATA_BINDINGS_INVALID")
    if data_bindings["calendar_sha256"] != canonical_sha256(trading_calendar_dates):
        raise EvaluationExecutionError("TRADING_CALENDAR_BINDING_MISMATCH")
    signal_bindings = candidate_signal.get("data_bindings")
    if not isinstance(signal_bindings, dict) or any(
        signal_bindings.get(name) != data_bindings[name]
        for name in ("canonical_panel_sha256", "calendar_sha256")
    ):
        raise EvaluationExecutionError("CANDIDATE_SIGNAL_DATA_BINDING_MISMATCH")
    try:
        parsed_signal = date.fromisoformat(signal_date)
        parsed_execute = date.fromisoformat(execute_date)
    except ValueError as error:
        raise EvaluationExecutionError("signal_date and execute_date must be ISO dates") from error
    try:
        calendar = [date.fromisoformat(value).isoformat() for value in trading_calendar_dates]
        signal_index = calendar.index(signal_date)
    except (ValueError, IndexError) as error:
        raise EvaluationExecutionError("signal_date is not in the bound trading calendar") from error
    if calendar != sorted(set(calendar)):
        raise EvaluationExecutionError("trading calendar must be strictly increasing")
    if signal_index + 1 >= len(calendar) or calendar[signal_index + 1] != execute_date:
        raise EvaluationExecutionError("execute_date must be the next bound trading date")
    nav = _decimal(nav_cny, "nav_cny")
    current_cash = _decimal(current_cash_cny, "current_cash_cny")
    if nav <= 0:
        raise EvaluationExecutionError("nav_cny must be positive")
    if set(target_weights) != set(CANDIDATE_SYMBOLS) | {"CASH"}:
        raise EvaluationExecutionError("target_weights must contain B1/B2 symbols and CASH")
    targets = {symbol: _decimal(weight, f"target {symbol}") for symbol, weight in target_weights.items()}
    if any(weight < 0 for weight in targets.values()) or abs(sum(targets.values()) - Decimal("1")) > Decimal("1e-9"):
        raise EvaluationExecutionError("target_weights must be nonnegative and sum to one")
    if targets["CASH"] < Decimal("0.1"):
        raise EvaluationExecutionError("target cash weight violates preregistered floor")
    if sum(targets[symbol] for symbol in EQUITY_SYMBOLS) > Decimal("0.7") + Decimal("1e-12"):
        raise EvaluationExecutionError("target equity weight violates preregistered cap")
    for symbol in CANDIDATE_SYMBOLS:
        if targets[symbol] > _asset_cap(symbol) + Decimal("1e-12"):
            raise EvaluationExecutionError(f"target asset cap violated:{symbol}")

    closes: dict[str, Decimal] = {}
    quantities: dict[str, int] = {}
    available: dict[str, int] = {}
    for symbol in CANDIDATE_SYMBOLS:
        close = _decimal(signal_closes.get(symbol), f"signal close {symbol}")
        if close <= 0:
            raise EvaluationExecutionError(f"signal close must be positive:{symbol}")
        closes[symbol] = close
        quantity = current_quantities.get(symbol, 0)
        if not isinstance(quantity, int) or quantity < 0:
            raise EvaluationExecutionError(f"current quantity invalid:{symbol}")
        quantities[symbol] = quantity
        available_quantity = available_to_sell_quantities.get(symbol, 0)
        if (
            not isinstance(available_quantity, int)
            or available_quantity < 0
            or available_quantity > quantity
        ):
            raise EvaluationExecutionError(f"available-to-sell quantity invalid:{symbol}")
        available[symbol] = available_quantity
    reconstructed_nav = current_cash + sum(
        Decimal(quantities[symbol]) * closes[symbol] for symbol in CANDIDATE_SYMBOLS
    )
    if abs(_money(reconstructed_nav) - _money(nav)) > Decimal("0.01"):
        raise EvaluationExecutionError("PRE_STATE_NAV_ACCOUNTING_MISMATCH")
    pre_state = {
        "cash_cny": float(_money(current_cash)),
        "quantities": quantities,
        "available_to_sell_quantities": available,
        "signal_closes": {symbol: float(_price(closes[symbol])) for symbol in CANDIDATE_SYMBOLS},
        "nav_cny": float(_money(nav)),
    }
    pre_state_sha256 = canonical_sha256(pre_state)

    orders: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for symbol in CANDIDATE_SYMBOLS:
        current_weight = Decimal(quantities[symbol]) * closes[symbol] / nav
        delta = targets[symbol] - current_weight
        if abs(delta) <= Decimal("1e-14"):
            continue
        side = "BUY" if delta > 0 else "SELL"
        full_exit = side == "SELL" and targets[symbol] == 0 and quantities[symbol] > 0
        hard_limit_repair = (
            side == "SELL"
            and current_weight > _asset_cap(symbol)
            and targets[symbol] <= _asset_cap(symbol)
        )
        desired_notional = abs(delta) * nav
        ordinary_gate = (
            abs(delta) + Decimal("1e-15") >= WEIGHT_BAND
            and desired_notional + Decimal("1e-9") >= MINIMUM_ORDER_NOTIONAL
        )
        if not (ordinary_gate or full_exit or hard_limit_repair):
            skipped.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "reason": "NO_TRADE_BAND_OR_MINIMUM_NOTIONAL",
                    "absolute_weight_delta": float(abs(delta)),
                    "desired_notional_cny": float(_money(desired_notional)),
                }
            )
            continue
        if full_exit:
            if available[symbol] != quantities[symbol]:
                raise EvaluationExecutionError(f"T_PLUS_ONE_FULL_EXIT_UNAVAILABLE:{symbol}")
            quantity = available[symbol]
        else:
            raw_lots = desired_notional / closes[symbol] / LOT_SIZE
            rounding = ROUND_CEILING if hard_limit_repair else ROUND_FLOOR
            quantity = int(raw_lots.to_integral_value(rounding=rounding)) * LOT_SIZE
            if side == "SELL":
                quantity = min(quantity, available[symbol])
        if quantity <= 0:
            skipped.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "reason": "ZERO_QUANTITY_AFTER_LOT_ROUNDING",
                    "absolute_weight_delta": float(abs(delta)),
                    "desired_notional_cny": float(_money(desired_notional)),
                }
            )
            continue
        actual_signal_notional = Decimal(quantity) * closes[symbol]
        if not (full_exit or hard_limit_repair) and actual_signal_notional < MINIMUM_ORDER_NOTIONAL:
            skipped.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "reason": "MINIMUM_NOTIONAL_FAILED_AFTER_LOT_ROUNDING",
                    "absolute_weight_delta": float(abs(delta)),
                    "actual_signal_notional_cny": float(_money(actual_signal_notional)),
                }
            )
            continue
        order_identity = {
            "candidate": candidate_signal.get("candidate"),
            "candidate_proposal_sha256": candidate_proposal_sha256,
            "execution_semantics": "TARGET_WEIGHT_REBALANCE",
            "frozen_signal_sha256": None,
            "signal_date": signal_date,
            "execute_date": execute_date,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "raw_open_limit": None,
            "pre_state_sha256": pre_state_sha256,
            "protocol_manifest_sha256": protocol.manifest_sha256,
        }
        orders.append(
            {
                "order_id": canonical_sha256(order_identity),
                "status": "PENDING_NEXT_OPEN",
                "signal_date": signal_date,
                "execute_date": execute_date,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "signal_close": float(_price(closes[symbol])),
                "desired_notional_cny": float(_money(desired_notional)),
                "gate_bypass": "FULL_EXIT" if full_exit else ("HARD_LIMIT_REPAIR" if hard_limit_repair else None),
            }
        )
    orders.sort(key=lambda order: (0 if order["side"] == "SELL" else 1, order["symbol"]))
    result = {
        "schema_version": 1,
        "status": "PLANNED",
        "candidate": candidate_signal.get("candidate"),
        "candidate_proposal_sha256": candidate_proposal_sha256,
        "execution_semantics": "TARGET_WEIGHT_REBALANCE",
        "frozen_signal_sha256": None,
        "signal_date": signal_date,
        "execute_date": execute_date,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "data_bindings": dict(sorted(data_bindings.items())),
        "pre_state": pre_state,
        "pre_state_sha256": pre_state_sha256,
        "orders": orders,
        "skipped": skipped,
        "claim_boundary": {"next_open_observed": False, "orders_filled": False},
    }
    result["plan_sha256"] = canonical_sha256(result)
    return result


def plan_frozen_b0_orders(
    *,
    frozen_signal: dict[str, Any],
    execute_date: str,
    trading_calendar_dates: list[str],
    data_bindings: dict[str, str],
    current_cash_cny: float,
    current_quantities: dict[str, int],
    available_to_sell_quantities: dict[str, int],
    signal_closes: dict[str, float],
    nav_cny: float,
    position_accounting: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan frozen B0 fixed quantities without applying B1/B2 rebalance gates."""
    protocol = load_candidate_protocol(DEFAULT_CANDIDATE_MANIFEST)
    claimed_signal_sha256 = frozen_signal.get("signal_sha256")
    unsigned_signal = {
        key: value for key, value in frozen_signal.items() if key != "signal_sha256"
    }
    if (
        frozen_signal.get("status") != "OK"
        or frozen_signal.get("candidate") != "B0"
        or frozen_signal.get("protocol_manifest_sha256") != protocol.manifest_sha256
        or not _is_sha256(claimed_signal_sha256)
        or canonical_sha256(unsigned_signal) != claimed_signal_sha256
        or not _is_sha256(frozen_signal.get("frozen_source_contract_sha256"))
    ):
        raise EvaluationExecutionError("FROZEN_B0_SIGNAL_PROTOCOL_MISMATCH")
    required_bindings = {
        "readiness_artifact_sha256",
        "canonical_panel_sha256",
        "source_registry_sha256",
        "calendar_sha256",
    }
    if set(data_bindings) != required_bindings or any(
        not _is_sha256(value) for value in data_bindings.values()
    ):
        raise EvaluationExecutionError("DATA_BINDINGS_INVALID")
    if data_bindings["calendar_sha256"] != canonical_sha256(trading_calendar_dates):
        raise EvaluationExecutionError("TRADING_CALENDAR_BINDING_MISMATCH")
    signal_bindings = frozen_signal.get("data_bindings")
    if not isinstance(signal_bindings, dict) or any(
        signal_bindings.get(name) != data_bindings[name]
        for name in ("canonical_panel_sha256", "calendar_sha256")
    ):
        raise EvaluationExecutionError("FROZEN_B0_SIGNAL_DATA_BINDING_MISMATCH")
    signal_date = str(frozen_signal.get("signal_date", ""))
    try:
        date.fromisoformat(signal_date)
        date.fromisoformat(execute_date)
        calendar = [date.fromisoformat(value).isoformat() for value in trading_calendar_dates]
        signal_index = calendar.index(signal_date)
    except (ValueError, IndexError) as error:
        raise EvaluationExecutionError("B0 signal or calendar date invalid") from error
    if calendar != sorted(set(calendar)):
        raise EvaluationExecutionError("trading calendar must be strictly increasing")
    if signal_index + 1 >= len(calendar) or calendar[signal_index + 1] != execute_date:
        raise EvaluationExecutionError("execute_date must be the next bound trading date")

    nav = _decimal(nav_cny, "nav_cny")
    current_cash = _decimal(current_cash_cny, "current_cash_cny")
    if nav <= 0 or current_cash < 0:
        raise EvaluationExecutionError("B0 pre-state cash/nav invalid")
    closes: dict[str, Decimal] = {}
    quantities: dict[str, int] = {}
    available: dict[str, int] = {}
    for symbol in CANDIDATE_SYMBOLS:
        close = _decimal(signal_closes.get(symbol), f"signal close {symbol}")
        quantity = current_quantities.get(symbol, 0)
        sellable = available_to_sell_quantities.get(symbol, 0)
        if close <= 0:
            raise EvaluationExecutionError(f"signal close must be positive:{symbol}")
        if not isinstance(quantity, int) or quantity < 0:
            raise EvaluationExecutionError(f"current quantity invalid:{symbol}")
        if not isinstance(sellable, int) or sellable < 0 or sellable > quantity:
            raise EvaluationExecutionError(f"available-to-sell quantity invalid:{symbol}")
        closes[symbol] = close
        quantities[symbol] = quantity
        available[symbol] = sellable
    reconstructed_nav = current_cash + sum(
        Decimal(quantities[symbol]) * closes[symbol] for symbol in CANDIDATE_SYMBOLS
    )
    if abs(_money(reconstructed_nav) - _money(nav)) > Decimal("0.01"):
        raise EvaluationExecutionError("PRE_STATE_NAV_ACCOUNTING_MISMATCH")
    normalized_accounting = _normalize_position_accounting(
        position_accounting, quantities
    )
    pre_state = {
        "cash_cny": float(_money(current_cash)),
        "quantities": quantities,
        "available_to_sell_quantities": available,
        "signal_closes": {
            symbol: float(_price(closes[symbol])) for symbol in CANDIDATE_SYMBOLS
        },
        "nav_cny": float(_money(nav)),
        "position_accounting": normalized_accounting,
    }
    pre_state_sha256 = canonical_sha256(pre_state)

    signal_orders = frozen_signal.get("orders")
    if not isinstance(signal_orders, list):
        raise EvaluationExecutionError("FROZEN_B0_SIGNAL_ORDERS_INVALID")
    orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in signal_orders:
        if not isinstance(item, dict):
            raise EvaluationExecutionError("FROZEN_B0_SIGNAL_ORDER_INVALID")
        symbol = str(item.get("symbol", ""))
        side = str(item.get("side", ""))
        quantity = item.get("quantity")
        signal_close = _decimal(item.get("signal_close"), f"B0 signal close {symbol}")
        raw_open_limit = _decimal(item.get("raw_open_limit"), f"B0 raw open limit {symbol}")
        if (
            symbol not in CANDIDATE_SYMBOLS
            or symbol in seen
            or side not in {"BUY", "SELL"}
            or not isinstance(quantity, int)
            or quantity <= 0
            or quantity % LOT_SIZE != 0
            or signal_close != closes[symbol]
            or raw_open_limit <= 0
            or (side == "SELL" and quantity > available[symbol])
        ):
            raise EvaluationExecutionError(f"FROZEN_B0_SIGNAL_ORDER_INVALID:{symbol}")
        seen.add(symbol)
        order_identity = {
            "candidate": "B0",
            "candidate_proposal_sha256": None,
            "execution_semantics": "FROZEN_FIXED_QUANTITY_RAW_OPEN_LIMIT",
            "frozen_signal_sha256": claimed_signal_sha256,
            "signal_date": signal_date,
            "execute_date": execute_date,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "raw_open_limit": float(_price(raw_open_limit)),
            "pre_state_sha256": pre_state_sha256,
            "protocol_manifest_sha256": protocol.manifest_sha256,
        }
        orders.append(
            {
                "order_id": canonical_sha256(order_identity),
                "status": "PENDING_NEXT_OPEN",
                "signal_date": signal_date,
                "execute_date": execute_date,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "signal_close": float(_price(signal_close)),
                "raw_open_limit": float(_price(raw_open_limit)),
                "signal_type": str(item.get("signal_type", "")),
                "score": float(_decimal(item.get("score", 0), f"B0 score {symbol}")),
                "gate_bypass": "FROZEN_B0_FIXED_QUANTITY",
            }
        )
    orders.sort(key=lambda order: (0 if order["side"] == "SELL" else 1, order["symbol"]))
    result = {
        "schema_version": 1,
        "status": "PLANNED",
        "candidate": "B0",
        "candidate_proposal_sha256": None,
        "execution_semantics": "FROZEN_FIXED_QUANTITY_RAW_OPEN_LIMIT",
        "frozen_signal_sha256": claimed_signal_sha256,
        "frozen_source_contract_sha256": frozen_signal["frozen_source_contract_sha256"],
        "signal_date": signal_date,
        "execute_date": execute_date,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "data_bindings": dict(sorted(data_bindings.items())),
        "pre_state": pre_state,
        "pre_state_sha256": pre_state_sha256,
        "orders": orders,
        "skipped": [],
        "claim_boundary": {"next_open_observed": False, "orders_filled": False},
    }
    result["plan_sha256"] = canonical_sha256(result)
    return result


def _verify_plan_integrity(plan: dict[str, Any]) -> None:
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not _is_sha256(claimed) or canonical_sha256(unsigned) != claimed:
        raise EvaluationExecutionError("PLAN_HASH_MISMATCH")
    orders = plan.get("orders")
    if not isinstance(orders, list):
        raise EvaluationExecutionError("PLAN_ORDERS_INVALID")
    expected_order = sorted(
        orders, key=lambda order: (0 if order.get("side") == "SELL" else 1, str(order.get("symbol")))
    )
    if orders != expected_order:
        raise EvaluationExecutionError("PLAN_ORDER_SEQUENCE_INVALID")
    for order in orders:
        quantity = order.get("quantity")
        if (
            order.get("status") != "PENDING_NEXT_OPEN"
            or order.get("signal_date") != plan.get("signal_date")
            or order.get("execute_date") != plan.get("execute_date")
            or order.get("symbol") not in CANDIDATE_SYMBOLS
            or order.get("side") not in {"BUY", "SELL"}
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise EvaluationExecutionError("PLAN_ORDER_STRUCTURE_INVALID")
        if order.get("gate_bypass") != "FULL_EXIT" and quantity % LOT_SIZE != 0:
            raise EvaluationExecutionError("PLAN_ORDER_LOT_INVALID")
        identity = {
            "candidate": plan.get("candidate"),
            "candidate_proposal_sha256": plan.get("candidate_proposal_sha256"),
            "execution_semantics": plan.get("execution_semantics"),
            "frozen_signal_sha256": plan.get("frozen_signal_sha256"),
            "signal_date": plan.get("signal_date"),
            "execute_date": plan.get("execute_date"),
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": quantity,
            "raw_open_limit": order.get("raw_open_limit"),
            "pre_state_sha256": plan.get("pre_state_sha256"),
            "protocol_manifest_sha256": plan.get("protocol_manifest_sha256"),
        }
        if order.get("order_id") != canonical_sha256(identity):
            raise EvaluationExecutionError("PLAN_ORDER_ID_MISMATCH")


def _trusted_open(
    symbol: str,
    observation: dict[str, Any],
    execute_date: str,
    execution_cutoff: datetime,
    source_registry: dict[str, Any],
) -> tuple[Decimal, list[str], list[str]]:
    if observation.get("trading_status") != "TRADING":
        raise EvaluationExecutionError(f"OPEN_TRADING_STATUS_NOT_TRADING:{symbol}")
    actions = observation.get("corporate_actions")
    if not isinstance(actions, list) or any(
        not isinstance(action, dict) or action.get("processed") is not True
        for action in actions
    ):
        raise EvaluationExecutionError(f"OPEN_CORPORATE_ACTION_UNPROCESSED:{symbol}")
    registry = {
        str(item.get("id")): item
        for item in source_registry.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    source_observations = observation.get("observations")
    if not isinstance(source_observations, list) or len(source_observations) < 2:
        raise EvaluationExecutionError(f"OPEN_SOURCE_COUNT_INSUFFICIENT:{symbol}")
    values: list[Decimal] = []
    source_ids: list[str] = []
    groups: set[str] = set()
    observed_values: list[str] = []
    for item in source_observations:
        if not isinstance(item, dict):
            raise EvaluationExecutionError(f"OPEN_SOURCE_OBSERVATION_INVALID:{symbol}")
        source_id = str(item.get("source_id", ""))
        source = registry.get(source_id)
        if source is None:
            raise EvaluationExecutionError(f"OPEN_SOURCE_UNREGISTERED:{symbol}:{source_id}")
        allowed_uses = source.get("allowed_uses")
        if not isinstance(allowed_uses, list) or "local_research_evaluation" not in allowed_uses:
            raise EvaluationExecutionError(f"OPEN_SOURCE_USE_NOT_ALLOWED:{symbol}:{source_id}")
        group = source.get("independence_group")
        if not group:
            raise EvaluationExecutionError(f"OPEN_SOURCE_GROUP_MISSING:{symbol}:{source_id}")
        raw_hash = item.get("raw_content_sha256")
        if not _is_sha256(raw_hash):
            raise EvaluationExecutionError(f"OPEN_RAW_HASH_INVALID:{symbol}:{source_id}")
        try:
            observed_at = datetime.fromisoformat(
                str(item.get("observed_at", "")).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise EvaluationExecutionError(f"OPEN_OBSERVED_AT_INVALID:{symbol}:{source_id}") from error
        if observed_at.tzinfo is None:
            raise EvaluationExecutionError(f"OPEN_OBSERVED_AT_TIMEZONE_MISSING:{symbol}:{source_id}")
        if (
            observed_at.date().isoformat() != execute_date
            or observed_at > execution_cutoff.astimezone(observed_at.tzinfo)
        ):
            raise EvaluationExecutionError(f"OPEN_NOT_AVAILABLE_AT_EXECUTION_CUTOFF:{symbol}:{source_id}")
        value = _decimal(item.get("value"), f"raw open {symbol} {source_id}")
        if value <= 0:
            raise EvaluationExecutionError(f"OPEN_PRICE_INVALID:{symbol}:{source_id}")
        values.append(value)
        source_ids.append(source_id)
        groups.add(str(group))
        observed_values.append(observed_at.isoformat())
    if len(set(source_ids)) < 2 or len(groups) < 2:
        raise EvaluationExecutionError(f"OPEN_SOURCE_INDEPENDENCE_INSUFFICIENT:{symbol}")
    if max(values) - min(values) > Decimal("0.000001"):
        raise EvaluationExecutionError(f"OPEN_SOURCE_PRICE_DISAGREEMENT:{symbol}")
    return sum(values) / len(values), sorted(set(source_ids)), sorted(observed_values)


def simulate_next_open(
    plan: dict[str, Any],
    *,
    open_observations: dict[str, dict[str, Any]],
    source_registry: dict[str, Any],
    execution_cutoff: str,
    cash_cny: float,
    current_quantities: dict[str, int],
    available_to_sell_quantities: dict[str, int],
    slippage_bps: int = 10,
    position_accounting: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Settle planned orders at the trusted next open, sells before buys."""
    _verify_plan_integrity(plan)
    if plan.get("status") != "PLANNED" or plan.get("claim_boundary", {}).get("next_open_observed"):
        raise EvaluationExecutionError("execution requires an unfilled PLANNED artifact")
    if slippage_bps not in {10, 25, 50}:
        raise EvaluationExecutionError("slippage_bps must be preregistered as 10, 25, or 50")
    try:
        cutoff = datetime.fromisoformat(execution_cutoff.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationExecutionError("execution_cutoff must be ISO-8601") from error
    if cutoff.tzinfo is None:
        raise EvaluationExecutionError("execution_cutoff must include timezone")
    if cutoff.date().isoformat() != plan.get("execute_date"):
        raise EvaluationExecutionError("execution_cutoff date must equal plan execute_date")
    if canonical_sha256(source_registry) != plan.get("data_bindings", {}).get("source_registry_sha256"):
        raise EvaluationExecutionError("SOURCE_REGISTRY_BINDING_MISMATCH")
    cash = _decimal(cash_cny, "cash_cny")
    quantities = {symbol: int(current_quantities.get(symbol, 0)) for symbol in CANDIDATE_SYMBOLS}
    available = {
        symbol: int(available_to_sell_quantities.get(symbol, 0))
        for symbol in CANDIDATE_SYMBOLS
    }
    if (
        cash < 0
        or any(quantity < 0 for quantity in quantities.values())
        or any(available[symbol] < 0 or available[symbol] > quantities[symbol] for symbol in CANDIDATE_SYMBOLS)
    ):
        raise EvaluationExecutionError("starting cash and quantities must be nonnegative")
    supplied_pre_state = {
        "cash_cny": float(_money(cash)),
        "quantities": quantities,
        "available_to_sell_quantities": available,
        "signal_closes": plan.get("pre_state", {}).get("signal_closes"),
        "nav_cny": plan.get("pre_state", {}).get("nav_cny"),
    }
    accounting: dict[str, dict[str, Any]] | None = None
    if "position_accounting" in plan.get("pre_state", {}):
        accounting = _normalize_position_accounting(
            position_accounting, quantities
        )
        supplied_pre_state["position_accounting"] = accounting
    if canonical_sha256(supplied_pre_state) != plan.get("pre_state_sha256"):
        raise EvaluationExecutionError("EXECUTION_PRE_STATE_BINDING_MISMATCH")
    fills: list[dict[str, Any]] = []
    cancellations: list[dict[str, Any]] = []
    total_commission = Decimal("0")
    total_slippage = Decimal("0")
    trusted_open_count = 0
    realized_pnl = Decimal("0")
    slip = Decimal(slippage_bps) / Decimal("10000")

    for order in plan.get("orders", []):
        symbol = str(order["symbol"])
        try:
            raw_open, source_ids, observed_values = _trusted_open(
                symbol,
                open_observations[symbol],
                str(order["execute_date"]),
                cutoff,
                source_registry,
            )
        except (KeyError, EvaluationExecutionError) as error:
            cancellations.append({**order, "status": "CANCELLED_UNTRUSTED_OPEN", "reason": str(error)})
            continue
        trusted_open_count += 1
        side = str(order["side"])
        quantity = int(order["quantity"])
        if order.get("raw_open_limit") is not None:
            raw_open_limit = _decimal(
                order["raw_open_limit"], f"raw open limit {symbol}"
            )
            limit_failed = (side == "BUY" and raw_open > raw_open_limit) or (
                side == "SELL" and raw_open < raw_open_limit
            )
            if limit_failed:
                cancellations.append(
                    {
                        **order,
                        "status": "CANCELLED_RAW_OPEN_LIMIT",
                        "reason": "RAW_OPEN_OUTSIDE_FROZEN_B0_LIMIT",
                        "raw_open": float(_price(raw_open)),
                    }
                )
                continue
        if side == "SELL" and quantity > available[symbol]:
            cancellations.append({**order, "status": "CANCELLED_T_PLUS_ONE", "reason": "INSUFFICIENT_AVAILABLE_T_PLUS_ONE_QUANTITY"})
            continue
        fill_price = raw_open * (Decimal("1") + slip if side == "BUY" else Decimal("1") - slip)
        notional = fill_price * quantity
        commission = max(_money(notional * COMMISSION_RATE), MINIMUM_COMMISSION)
        slippage_cost = _money(abs(fill_price - raw_open) * quantity)
        if side == "BUY":
            total_cash = _money(notional) + commission
            if total_cash > cash:
                cancellations.append({**order, "status": "CANCELLED_INSUFFICIENT_CASH", "reason": "INSUFFICIENT_CASH_AT_TRUSTED_OPEN"})
                continue
            old_quantity = quantities[symbol]
            cash -= total_cash
            quantities[symbol] += quantity
            if accounting is not None:
                old = accounting.get(symbol)
                old_cost = (
                    Decimal(old_quantity) * _decimal(old["average_cost"], "average cost")
                    if old is not None
                    else Decimal("0")
                )
                acquired_date = (
                    str(old["acquired_date"])
                    if old is not None
                    else str(order["execute_date"])
                )
                accounting[symbol] = {
                    "quantity": quantities[symbol],
                    "average_cost": float(
                        _price((old_cost + total_cash) / quantities[symbol])
                    ),
                    "acquired_date": acquired_date,
                    "last_buy_date": str(order["execute_date"]),
                }
        elif side == "SELL":
            if accounting is not None:
                cost_basis = _decimal(
                    accounting[symbol]["average_cost"], "average cost"
                ) * quantity
                realized_pnl += _money(notional) - commission - cost_basis
            cash += _money(notional) - commission
            quantities[symbol] -= quantity
            available[symbol] -= quantity
            if accounting is not None:
                if quantities[symbol] == 0:
                    del accounting[symbol]
                else:
                    accounting[symbol]["quantity"] = quantities[symbol]
        else:
            cancellations.append({**order, "status": "CANCELLED_INVALID", "reason": "INVALID_ORDER_SIDE"})
            continue
        total_commission += commission
        total_slippage += slippage_cost
        fills.append(
            {
                **order,
                "status": "FILLED",
                "raw_open": float(_price(raw_open)),
                "fill_price": float(_price(fill_price)),
                "notional_cny": float(_money(notional)),
                "commission_cny": float(commission),
                "slippage_cny": float(slippage_cost),
                "total_fees_cny": float(commission),
                "fill_as_of": execution_cutoff,
                "open_observed_at": observed_values,
                "open_source_ids": source_ids,
            }
        )
    after_state = {
        "cash_cny": float(_money(cash)),
        "quantities": quantities,
        "available_to_sell_quantities": available,
    }
    if accounting is not None:
        after_state["position_accounting"] = accounting
        after_state["realized_pnl_cny"] = float(_money(realized_pnl))
    if fills and not cancellations:
        execution_status = "FILLED"
    elif fills:
        execution_status = "PARTIAL"
    else:
        execution_status = "ALL_CANCELLED"
    return {
        "schema_version": 1,
        "status": execution_status,
        "execution_attempted": True,
        "trusted_open_count": trusted_open_count,
        "after_state": after_state,
        "after_state_sha256": canonical_sha256(after_state),
        "fills": fills,
        "cancellations": cancellations,
        "commission_cny": float(_money(total_commission)),
        "slippage_cny": float(_money(total_slippage)),
        "claim_boundary": {
            "simulation_only": True,
            "next_open_observed": trusted_open_count > 0,
            "performance_claim_authorized": False,
        },
    }
