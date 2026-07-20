from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import pstdev
from typing import Any


DEFAULT_LEDGER = Path("data/ledger/portfolio.json")
DEFAULT_ORDERS_LOG = Path("data/ledger/orders.jsonl")
DEFAULT_REPORT_DIR = Path("reports/daily")
DEFAULT_EXECUTION_REPORT_DIR = Path("reports/execution")
DEFAULT_FUND_REPORT_DIR = Path("reports/funds")
DEFAULT_STRATEGY = Path("config/strategy.json")
CENT = Decimal("0.01")
OPEN_END_FUND_TYPES = {
    "open_end_index_fund",
    "open_end_active_fund",
    "open_end_bond_fund",
    "open_end_gold_fund",
    "money_market_fund",
}
EQUITY_EXPOSURE_TYPES = {"stock", "equity_etf", "open_end_index_fund", "open_end_active_fund"}
FUND_ASSET_TYPES = OPEN_END_FUND_TYPES | {"equity_etf", "cash_etf", "bond_etf", "gold_etf"}
AGGREGATOR_SOURCE_IDS = {"eastmoney", "sina_finance", "tencent_finance", "investing"}


class DataGateError(ValueError):
    """Raised when point-in-time data is unsafe for decision generation."""


def _money(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise DataGateError(f"{path} 顶层必须是 JSON object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def initialize_ledger(path: Path = DEFAULT_LEDGER) -> dict[str, Any]:
    if path.exists():
        return {"status": "EXISTS", "ledger": str(path)}
    ledger = {
        "schema_version": 1,
        "initialized_at": "2026-07-19T00:00:00+08:00",
        "initial_project_capital_cny": 30000.0,
        "cash_cny": 29900.0,
        "positions": {},
        "pending_orders": [],
        "research_infrastructure": {
            "provider": "DeepSeek",
            "reserved_cny": 100.0,
            "spent_cny": 0.0,
            "balance_basis": "USER_STATED_NOT_API_VERIFIED",
            "actual_calls": 0,
        },
        "last_run_id": None,
        "run_history": [],
    }
    _atomic_json(path, ledger)
    return {"status": "CREATED", "ledger": str(path)}


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataGateError(f"无效 ISO 时间: {value}") from exc
    if parsed.tzinfo is None:
        raise DataGateError("as_of 必须含时区")
    return parsed


def validate_snapshot(
    snapshot: dict[str, Any], strategy: dict[str, Any], now: datetime | None = None
) -> list[str]:
    required = ("schema_version", "run_date", "as_of", "is_trading_day", "indices", "assets", "evidence")
    missing = [key for key in required if key not in snapshot]
    if missing:
        raise DataGateError(f"快照缺少字段: {', '.join(missing)}")
    if snapshot["schema_version"] != 1:
        raise DataGateError("不支持的快照 schema_version")
    try:
        datetime.fromisoformat(snapshot["run_date"])
    except ValueError as exc:
        raise DataGateError("run_date 必须是 YYYY-MM-DD") from exc
    as_of = _parse_time(snapshot["as_of"])
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if as_of > current.astimezone(as_of.tzinfo):
        raise DataGateError("as_of 位于未来，触发前视偏差门禁")

    warnings: list[str] = []
    max_age_hours = float(strategy["data_gates"]["max_snapshot_age_hours"])
    age_hours = (current.astimezone(as_of.tzinfo) - as_of).total_seconds() / 3600
    if age_hours > max_age_hours:
        warnings.append(f"STALE_SNAPSHOT:{age_hours:.1f}h")

    seen: set[str] = set()
    for asset in snapshot["assets"]:
        symbol = str(asset.get("symbol", ""))
        if not symbol or symbol in seen:
            raise DataGateError(f"资产代码缺失或重复: {symbol!r}")
        seen.add(symbol)
        if float(asset.get("close", 0)) <= 0:
            raise DataGateError(f"{symbol} close 必须为正数")
        if not asset.get("source_ids"):
            raise DataGateError(f"{symbol} 缺少 source_ids")
        history = asset.get("history", [])
        if history and abs(float(history[-1]) - float(asset["close"])) > 1e-8:
            raise DataGateError(f"{symbol} history 末值与 close 不一致")
        corporate_actions = asset.get("corporate_actions", [])
        if not isinstance(corporate_actions, list):
            raise DataGateError(f"{symbol} corporate_actions 必须是 list")
        adjusted = asset.get("history_adjusted_for_corporate_actions", False)
        if not isinstance(adjusted, bool):
            raise DataGateError(
                f"{symbol} history_adjusted_for_corporate_actions 必须是 boolean"
            )
        for action in corporate_actions:
            if not isinstance(action, dict):
                raise DataGateError(f"{symbol} corporate_actions 元素必须是 object")
            missing_action = [
                key for key in ("date", "type", "source_ids") if not action.get(key)
            ]
            if missing_action:
                raise DataGateError(
                    f"{symbol} corporate action 缺少字段: {', '.join(missing_action)}"
                )
            try:
                datetime.fromisoformat(str(action["date"]))
            except ValueError as exc:
                raise DataGateError(
                    f"{symbol} corporate action date 必须是 YYYY-MM-DD"
                ) from exc
            if not isinstance(action["source_ids"], list):
                raise DataGateError(
                    f"{symbol} corporate action source_ids 必须是 list"
                )
        if "open_source_ids" in asset and not isinstance(
            asset["open_source_ids"], list
        ):
            raise DataGateError(f"{symbol} open_source_ids 必须是 list")
        if "fund_metadata" in asset and not isinstance(asset["fund_metadata"], dict):
            raise DataGateError(f"{symbol} fund_metadata 必须是 object")
        for field in ("risk_bucket", "exposure_group"):
            if field in asset and not isinstance(asset[field], str):
                raise DataGateError(f"{symbol} {field} 必须是 string")
    return warnings


def validate_snapshot_file(input_path: Path, strategy_path: Path = DEFAULT_STRATEGY) -> dict[str, Any]:
    snapshot = _read_json(input_path)
    strategy = _read_json(strategy_path)
    warnings = validate_snapshot(snapshot, strategy)
    return {
        "status": "PASS",
        "input": str(input_path),
        "sha256": _sha256(input_path),
        "warnings": warnings,
    }


def _returns(prices: list[float]) -> list[float]:
    return [prices[index] / prices[index - 1] - 1 for index in range(1, len(prices))]


def _max_drawdown(prices: list[float]) -> float:
    peak = prices[0]
    drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)
    return drawdown


def _metrics(asset: dict[str, Any]) -> dict[str, float] | None:
    prices = [float(value) for value in asset.get("history", [])]
    if len(prices) < 2:
        return None
    daily = _returns(prices)
    return {
        "ma5": sum(prices[-5:]) / min(5, len(prices)),
        "ma20": sum(prices[-20:]) / min(20, len(prices)),
        "volatility20": pstdev(daily[-20:]) * math.sqrt(252) if len(daily) > 1 else 0.0,
        "max_drawdown20": _max_drawdown(prices[-20:]),
        "return20": prices[-1] / prices[-min(20, len(prices))] - 1,
    }


def _project_value(ledger: dict[str, Any], assets: dict[str, dict[str, Any]]) -> dict[str, float]:
    positions_value = Decimal("0")
    for symbol, position in ledger["positions"].items():
        price = assets.get(symbol, {}).get("close", position["average_cost"])
        positions_value += _money(Decimal(str(price)) * Decimal(str(position["quantity"])))
    cash = _money(ledger["cash_cny"])
    infra = ledger["research_infrastructure"]
    infrastructure_remaining = _money(infra["reserved_cny"]) - _money(infra["spent_cny"])
    return {
        "cash_cny": float(cash),
        "positions_cny": float(positions_value),
        "investable_value_cny": float(cash + positions_value),
        "infrastructure_remaining_cny": float(infrastructure_remaining),
        "project_equity_cny": float(cash + positions_value + infrastructure_remaining),
    }


def _trade_fees(
    notional: Decimal,
    asset_type: str,
    side: str,
    strategy: dict[str, Any],
) -> dict[str, Decimal]:
    """Return investor-facing simulated fees without double-counting exchange charges."""
    costs = strategy["costs"]
    commission = max(
        _money(costs["minimum_commission_cny"]),
        _money(notional * Decimal(str(costs["commission_rate"]))),
    )
    transfer_fee = Decimal("0.00")
    stamp_tax = Decimal("0.00")
    if asset_type == "stock":
        transfer_fee = _money(
            notional * Decimal(str(costs.get("stock_transfer_fee_rate", 0)))
        )
        if side == "SELL":
            stamp_tax = _money(
                notional * Decimal(str(costs["stock_sell_stamp_rate"]))
            )
    return {
        "commission_cny": commission,
        "transfer_fee_cny": transfer_fee,
        "stamp_tax_cny": stamp_tax,
        "total_fees_cny": commission + transfer_fee + stamp_tax,
    }


def _settle_pending(
    ledger: dict[str, Any],
    snapshot: dict[str, Any],
    strategy: dict[str, Any],
    *,
    allowed_market_states: set[str] | None = None,
    require_open_source_ids: bool = False,
) -> list[dict[str, Any]]:
    allowed_states = allowed_market_states or {"closed"}
    if not snapshot["is_trading_day"] or snapshot.get("market_state") not in allowed_states:
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    events: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for order in ledger["pending_orders"]:
        if order.get("status") != "PENDING_NEXT_OPEN":
            still_pending.append(order)
            continue
        asset = assets.get(order["symbol"])
        if snapshot["run_date"] <= order["signal_date"]:
            still_pending.append(order)
            continue
        if not asset:
            if require_open_source_ids:
                order["status"] = "CANCELLED_DATA_GATE"
                order["cancellation_reason"] = "ASSET_MISSING_FROM_OPEN_SNAPSHOT"
                events.append(order)
            else:
                still_pending.append(order)
            continue
        if asset.get("corporate_actions") and not asset.get(
            "history_adjusted_for_corporate_actions", False
        ):
            order["status"] = "CANCELLED_DATA_GATE"
            order["cancellation_reason"] = "UNADJUSTED_CORPORATE_ACTION"
            events.append(order)
            continue
        if asset.get("trading_status", "TRADING") != "TRADING":
            order["status"] = "CANCELLED_DATA_GATE"
            order["cancellation_reason"] = str(
                asset.get("trading_status", "NOT_TRADING")
            )
            events.append(order)
            continue
        open_sources = asset.get("open_source_ids")
        if open_sources is None and not require_open_source_ids:
            open_sources = asset.get("source_ids", [])
        if asset.get("open") is None or len(set(open_sources or [])) < 2:
            order["status"] = "CANCELLED_DATA_GATE"
            order["cancellation_reason"] = "OPEN_PRICE_NOT_CROSSCHECKED"
            events.append(order)
            continue
        price = float(asset["open"])
        quantity = int(order["quantity"])
        notional = _money(price * quantity)
        fees = _trade_fees(notional, str(asset["asset_type"]), str(order["side"]), strategy)
        if order["side"] == "BUY":
            total = notional + fees["total_fees_cny"]
            if price > float(order["limit_price"]) or total > _money(ledger["cash_cny"]):
                order["status"] = "CANCELLED_LIMIT_OR_CASH"
                order["cancellation_reason"] = (
                    "OPEN_ABOVE_LIMIT"
                    if price > float(order["limit_price"])
                    else "INSUFFICIENT_CASH"
                )
                events.append(order)
                continue
            position = ledger["positions"].get(order["symbol"], {"quantity": 0, "average_cost": 0.0})
            old_cost = _money(position["average_cost"] * position["quantity"])
            new_quantity = int(position["quantity"]) + quantity
            position["quantity"] = new_quantity
            position["average_cost"] = float((old_cost + total) / new_quantity)
            position["name"] = asset["name"]
            position["asset_type"] = asset["asset_type"]
            position["risk_bucket"] = asset.get(
                "risk_bucket", order.get("risk_bucket", _default_risk_bucket(asset))
            )
            position["exposure_group"] = asset.get(
                "exposure_group", order.get("exposure_group", order["symbol"])
            )
            ledger["positions"][order["symbol"]] = position
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) - total)
        else:
            position = ledger["positions"].get(order["symbol"])
            if not position or quantity > int(position["quantity"]):
                order["status"] = "CANCELLED_POSITION"
                order["cancellation_reason"] = "INSUFFICIENT_POSITION"
                events.append(order)
                continue
            ledger["cash_cny"] = float(
                _money(ledger["cash_cny"]) + notional - fees["total_fees_cny"]
            )
            position["quantity"] = int(position["quantity"]) - quantity
            if position["quantity"] == 0:
                del ledger["positions"][order["symbol"]]
        order["status"] = "FILLED"
        order["fill_date"] = snapshot["run_date"]
        order["fill_as_of"] = snapshot["as_of"]
        order["fill_price"] = price
        order["commission_cny"] = float(fees["commission_cny"])
        order["transfer_fee_cny"] = float(fees["transfer_fee_cny"])
        order["stamp_tax_cny"] = float(fees["stamp_tax_cny"])
        order["total_fees_cny"] = float(fees["total_fees_cny"])
        order["fee_model_version"] = str(strategy["version"])
        events.append(order)
    ledger["pending_orders"] = still_pending
    return events


def _market_shock(snapshot: dict[str, Any], strategy: dict[str, Any]) -> bool:
    threshold = float(strategy["risk"]["broad_market_shock_daily_return"])
    return any(
        item.get("broad", False) and float(item["daily_return"]) <= threshold
        for item in snapshot["indices"]
    )


def _is_equity_exposure(asset: dict[str, Any]) -> bool:
    return str(asset.get("asset_type")) in EQUITY_EXPOSURE_TYPES


def _default_risk_bucket(asset: dict[str, Any]) -> str:
    asset_type = str(asset.get("asset_type"))
    if asset_type in {"cash_etf", "money_market_fund"}:
        return "cash_management"
    if asset_type in {"bond_etf", "open_end_bond_fund"}:
        return "fixed_income"
    if asset_type in {"gold_etf", "open_end_gold_fund"}:
        return "gold"
    return "unclassified_equity"


def _fund_gate_reasons(asset: dict[str, Any], strategy: dict[str, Any]) -> list[str]:
    asset_type = str(asset.get("asset_type"))
    if asset_type not in FUND_ASSET_TYPES:
        return []
    rules = strategy.get("data_gates", {}).get("fund", {})
    sources = set(asset.get("source_ids", []))
    reasons: list[str] = []
    if rules.get("require_tiantian_crosscheck", False) and "eastmoney" not in sources:
        reasons.append("TIANTIAN_CROSSCHECK_MISSING")
    if rules.get("require_fund_company_or_exchange_source", False):
        primary_sources = set(asset.get("primary_source_ids", []))
        primary_sources.update(source for source in sources if source not in AGGREGATOR_SOURCE_IDS)
        if not primary_sources:
            reasons.append("FUND_PRIMARY_SOURCE_MISSING")

    if asset_type not in OPEN_END_FUND_TYPES:
        return reasons
    metadata = asset.get("fund_metadata") or {}
    required = {
        "nav_date": "FUND_NAV_DATE_MISSING",
        "nav_age_trading_days": "FUND_NAV_AGE_MISSING",
        "subscription_status": "FUND_SUBSCRIPTION_STATUS_MISSING",
        "redemption_status": "FUND_REDEMPTION_STATUS_MISSING",
        "aum_cny": "FUND_AUM_MISSING",
        "fees_verified": "FUND_FEES_NOT_VERIFIED",
        "purchase_fee_rate": "FUND_PURCHASE_FEE_MISSING",
        "redemption_fee_rate": "FUND_REDEMPTION_FEE_MISSING",
    }
    for field, reason in required.items():
        if field not in metadata:
            reasons.append(reason)
    if metadata.get("nav_age_trading_days", 10**9) > int(
        rules.get("maximum_nav_age_trading_days", 2)
    ):
        reasons.append("FUND_NAV_STALE")
    allowed = set(rules.get("allowed_subscription_statuses", ["OPEN"]))
    if metadata.get("subscription_status") not in allowed:
        reasons.append("FUND_SUBSCRIPTION_NOT_OPEN")
    if metadata.get("redemption_status") != "OPEN":
        reasons.append("FUND_REDEMPTION_NOT_OPEN")
    if float(metadata.get("aum_cny", 0)) < float(rules.get("minimum_aum_cny", 0)):
        reasons.append("FUND_AUM_TOO_SMALL")
    if asset_type == "open_end_active_fund":
        if int(metadata.get("manager_tenure_days", 0)) < int(
            rules.get("active_manager_minimum_tenure_days", 365)
        ):
            reasons.append("FUND_MANAGER_TENURE_TOO_SHORT")
        if int(metadata.get("holdings_age_days", 10**9)) > int(
            rules.get("maximum_holdings_age_days", 120)
        ):
            reasons.append("FUND_HOLDINGS_STALE")
        if metadata.get("style_drift_status") != "STABLE":
            reasons.append("FUND_STYLE_DRIFT_NOT_CLEARED")
    if asset.get("order_engine") != "next_confirmed_nav":
        reasons.append("OPEN_END_FUND_EXECUTION_NOT_IMPLEMENTED")
    return sorted(set(reasons))


def _recommendations(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any], warnings: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    shock = _market_shock(snapshot, strategy)
    global_blocks = list(warnings)
    if not snapshot["is_trading_day"]:
        global_blocks.append("NON_TRADING_DAY")
    if shock:
        global_blocks.append("BROAD_MARKET_SHOCK")
    recommendations: list[dict[str, Any]] = []
    min_sources = int(strategy["data_gates"]["minimum_price_sources"])
    min_history = int(strategy["data_gates"]["minimum_history_points"])
    for asset in snapshot["assets"]:
        symbol = str(asset["symbol"])
        asset_type = str(asset["asset_type"])
        metrics = _metrics(asset)
        reasons: list[str] = _fund_gate_reasons(asset, strategy)
        action = "WATCH"
        target_weight = 0.0
        held = symbol in ledger["positions"]
        if len(set(asset.get("source_ids", []))) < min_sources:
            reasons.append("PRICE_NOT_CROSSCHECKED")
        if metrics is None or len(asset.get("history", [])) < min_history:
            reasons.append("INSUFFICIENT_HISTORY")
        if asset.get("corporate_actions") and not asset.get(
            "history_adjusted_for_corporate_actions", False
        ):
            reasons.append("UNADJUSTED_CORPORATE_ACTION")
        if any(
            reason in reasons
            for reason in (
                "PRICE_NOT_CROSSCHECKED",
                "INSUFFICIENT_HISTORY",
                "UNADJUSTED_CORPORATE_ACTION",
                "TIANTIAN_CROSSCHECK_MISSING",
                "FUND_PRIMARY_SOURCE_MISSING",
                "FUND_NAV_DATE_MISSING",
                "FUND_NAV_AGE_MISSING",
                "FUND_NAV_STALE",
                "FUND_SUBSCRIPTION_STATUS_MISSING",
                "FUND_SUBSCRIPTION_NOT_OPEN",
                "FUND_REDEMPTION_STATUS_MISSING",
                "FUND_REDEMPTION_NOT_OPEN",
                "FUND_AUM_MISSING",
                "FUND_AUM_TOO_SMALL",
                "FUND_FEES_NOT_VERIFIED",
                "FUND_PURCHASE_FEE_MISSING",
                "FUND_REDEMPTION_FEE_MISSING",
                "FUND_MANAGER_TENURE_TOO_SHORT",
                "FUND_HOLDINGS_STALE",
                "FUND_STYLE_DRIFT_NOT_CLEARED",
                "OPEN_END_FUND_EXECUTION_NOT_IMPLEMENTED",
            )
        ):
            action = "HOLD" if held else "WATCH"
        elif held and float(asset["close"]) < metrics["ma20"] * float(strategy["risk"]["exit_below_ma20_ratio"]):
            action = "SELL"
            reasons.append("TREND_EXIT")
        elif asset_type in {"cash_etf", "money_market_fund"}:
            if metrics["volatility20"] <= float(strategy["risk"]["cash_etf_max_volatility"]):
                action = "HOLD" if held else "BUY"
                target_weight = float(strategy["risk"]["cash_etf_target_weight"])
                reasons.append("CASH_MANAGEMENT_ELIGIBLE")
            else:
                reasons.append("CASH_ETF_VOLATILITY_TOO_HIGH")
        elif asset_type in {"bond_etf", "open_end_bond_fund"}:
            if float(asset["close"]) >= metrics["ma20"] and metrics["return20"] >= -0.005:
                action = "HOLD" if held else "BUY"
                target_weight = float(strategy["risk"]["bond_fund_target_weight"])
                reasons.append("FIXED_INCOME_TREND_ELIGIBLE")
            else:
                action = "HOLD" if held else "WATCH"
                reasons.append("FIXED_INCOME_TREND_NOT_CONFIRMED")
        elif asset_type in {"gold_etf", "open_end_gold_fund"}:
            if float(asset["close"]) > metrics["ma20"] and metrics["ma5"] >= metrics["ma20"]:
                action = "HOLD" if held else "BUY"
                target_weight = float(strategy["risk"]["gold_fund_target_weight"])
                reasons.append("GOLD_DIVERSIFIER_TREND_ELIGIBLE")
            else:
                action = "HOLD" if held else "WATCH"
                reasons.append("GOLD_TREND_NOT_CONFIRMED")
        elif shock and _is_equity_exposure(asset):
            action = "HOLD" if held else "WATCH"
            reasons.append("EQUITY_BUY_BLOCKED_BY_MARKET_SHOCK")
        elif (
            float(asset["close"]) > metrics["ma20"]
            and metrics["ma5"] >= metrics["ma20"]
            and float(asset.get("daily_return", 0)) > float(strategy["risk"]["single_asset_shock_daily_return"])
        ):
            action = "HOLD" if held else "BUY"
            target_weight = float(strategy["risk"]["max_single_etf_weight"])
            if asset_type == "stock":
                target_weight = float(strategy["risk"]["max_single_stock_weight"])
            elif asset_type in OPEN_END_FUND_TYPES:
                target_weight = float(strategy["risk"]["max_single_open_end_fund_weight"])
            reasons.append("POSITIVE_TREND_WITHOUT_SHOCK")
        else:
            action = "HOLD" if held else "WATCH"
            reasons.append("TREND_NOT_CONFIRMED")
        recommendations.append(
            {
                "symbol": symbol,
                "name": asset["name"],
                "asset_type": asset["asset_type"],
                "risk_bucket": asset.get("risk_bucket", _default_risk_bucket(asset)),
                "exposure_group": asset.get("exposure_group", symbol),
                "action": action,
                "target_weight": target_weight,
                "reasons": reasons,
                "metrics": metrics,
                "source_count": len(set(asset.get("source_ids", []))),
            }
        )
    return recommendations, sorted(set(global_blocks))


def _create_orders(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any], recommendations: list[dict[str, Any]], blocks: list[str]
) -> list[dict[str, Any]]:
    hard_blocks = [block for block in blocks if block != "BROAD_MARKET_SHOCK"]
    if hard_blocks or not snapshot["is_trading_day"] or snapshot.get("market_state") != "closed":
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets)
    portfolio_value = float(values["investable_value_cny"])
    diversification = strategy.get("diversification", {})
    bucket_caps = diversification.get("bucket_weight_caps", {})
    max_positions = int(diversification.get("maximum_positions", 10**9))
    max_new_buys = int(diversification.get("maximum_new_buys_per_cycle", 10**9))
    one_per_group = bool(diversification.get("one_position_per_exposure_group", False))
    minimum_cash_value = portfolio_value * float(strategy["risk"].get("minimum_cash_weight", 0))
    existing = {order["symbol"] for order in ledger["pending_orders"]}
    used_symbols = set(ledger["positions"]) | existing
    used_groups: set[str] = set()
    bucket_values: dict[str, float] = {}
    equity_value = 0.0
    pending_buy_value = 0.0

    for symbol, position in ledger["positions"].items():
        asset = assets.get(symbol, position)
        price = float(assets.get(symbol, {}).get("close", position["average_cost"]))
        market_value = price * int(position["quantity"])
        bucket = str(position.get("risk_bucket", asset.get("risk_bucket", _default_risk_bucket(asset))))
        group = str(position.get("exposure_group", asset.get("exposure_group", symbol)))
        bucket_values[bucket] = bucket_values.get(bucket, 0.0) + market_value
        used_groups.add(group)
        if _is_equity_exposure(asset):
            equity_value += market_value
    for pending in ledger["pending_orders"]:
        if pending.get("side") != "BUY" or pending.get("status") not in {
            "PENDING_NEXT_OPEN",
            "PENDING_NEXT_NAV",
        }:
            continue
        symbol = str(pending["symbol"])
        asset = assets.get(symbol, pending)
        if pending["status"] == "PENDING_NEXT_NAV":
            planned_value = float(pending["amount_cny"])
        else:
            planned_value = float(pending["signal_close"]) * int(pending["quantity"])
        bucket = str(pending.get("risk_bucket", asset.get("risk_bucket", _default_risk_bucket(asset))))
        group = str(pending.get("exposure_group", asset.get("exposure_group", symbol)))
        bucket_values[bucket] = bucket_values.get(bucket, 0.0) + planned_value
        used_groups.add(group)
        pending_buy_value += planned_value
        if _is_equity_exposure(asset):
            equity_value += planned_value

    orders: list[dict[str, Any]] = []
    buy_count = sum(
        order.get("side") == "BUY"
        and order.get("status") in {"PENDING_NEXT_OPEN", "PENDING_NEXT_NAV"}
        and str(order.get("signal_date")) == str(snapshot["run_date"])
        for order in ledger["pending_orders"]
    )

    def block_recommendation(item: dict[str, Any], reason: str) -> None:
        item["action"] = "WATCH"
        item["target_weight"] = 0.0
        if reason not in item["reasons"]:
            item["reasons"].append(reason)

    for recommendation in recommendations:
        symbol = recommendation["symbol"]
        if symbol in existing or recommendation["action"] not in ("BUY", "SELL"):
            continue
        asset = assets[symbol]
        if str(asset.get("asset_type")) in OPEN_END_FUND_TYPES:
            block_recommendation(recommendation, "USE_FUND_NAV_PIPELINE")
            continue
        bucket = str(asset.get("risk_bucket", _default_risk_bucket(asset)))
        group = str(asset.get("exposure_group", symbol))
        lot = int(asset.get("lot_size", 100))
        current_quantity = int(ledger["positions"].get(symbol, {}).get("quantity", 0))
        if recommendation["action"] == "BUY":
            if buy_count >= max_new_buys:
                block_recommendation(recommendation, "MAXIMUM_NEW_BUYS_REACHED")
                continue
            if len(used_symbols) >= max_positions:
                block_recommendation(recommendation, "MAXIMUM_POSITIONS_REACHED")
                continue
            if one_per_group and group in used_groups:
                block_recommendation(recommendation, "DUPLICATE_EXPOSURE_GROUP")
                continue
            desired_value = values["investable_value_cny"] * recommendation["target_weight"]
            bucket_cap_value = portfolio_value * float(bucket_caps.get(bucket, 1.0))
            desired_value = min(
                desired_value,
                max(0.0, bucket_cap_value - bucket_values.get(bucket, 0.0)),
            )
            if _is_equity_exposure(asset):
                equity_cap_value = portfolio_value * float(strategy["risk"]["max_total_equity_weight"])
                desired_value = min(desired_value, max(0.0, equity_cap_value - equity_value))
            purchase_capacity = max(
                0.0,
                float(ledger["cash_cny"]) - pending_buy_value - minimum_cash_value,
            )
            desired_value = min(desired_value, purchase_capacity)
            desired_quantity = math.floor(desired_value / float(asset["close"]) / lot) * lot
            quantity = max(0, desired_quantity - current_quantity)
            if quantity == 0:
                block_recommendation(recommendation, "POSITION_TOO_SMALL_AFTER_DIVERSIFICATION")
                continue
            planned_value = float(asset["close"]) * quantity
            recommendation["target_weight"] = planned_value / portfolio_value if portfolio_value else 0.0
            limit = float(asset["close"]) * (1 + float(strategy["risk"]["maximum_next_open_gap"]))
            side = "BUY"
            buy_count += 1
            pending_buy_value += planned_value
            bucket_values[bucket] = bucket_values.get(bucket, 0.0) + planned_value
            if _is_equity_exposure(asset):
                equity_value += planned_value
            used_symbols.add(symbol)
            used_groups.add(group)
        else:
            quantity = current_quantity
            if quantity == 0:
                continue
            limit = 0.0
            side = "SELL"
        order = {
            "order_id": uuid.uuid4().hex,
            "status": "PENDING_NEXT_OPEN",
            "side": side,
            "symbol": symbol,
            "name": asset["name"],
            "quantity": quantity,
            "lot_size": lot,
            "risk_bucket": bucket,
            "exposure_group": group,
            "signal_date": snapshot["run_date"],
            "signal_as_of": snapshot["as_of"],
            "signal_close": float(asset["close"]),
            "limit_price": round(limit, 6),
            "simulation_only": True,
            "reasons": recommendation["reasons"],
        }
        ledger["pending_orders"].append(order)
        orders.append(order)
    return orders


def _fmt_pct(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value * 100:.2f}%"


def _render_report(
    snapshot: dict[str, Any], ledger: dict[str, Any], values: dict[str, float], recommendations: list[dict[str, Any]], blocks: list[str], fills: list[dict[str, Any]], orders: list[dict[str, Any]], input_hash: str, mode: str
) -> str:
    status = "ORDERS_PENDING" if orders else ("FILLED" if fills else "NO_TRADE")
    lines = [
        f"# Vibe Finance {snapshot['run_date']} {'短周期' if mode == 'short' else '长周期'}报告",
        "",
        f"- 状态：`{status}`",
        f"- 证据截点：{snapshot['as_of']}",
        f"- 输入 SHA-256：`{input_hash}`",
        f"- 项目权益：{values['project_equity_cny']:.2f} 元",
        f"- 可投资现金：{values['cash_cny']:.2f} 元",
        f"- 持仓市值：{values['positions_cny']:.2f} 元",
        f"- DeepSeek 预留余额：{values['infrastructure_remaining_cny']:.2f} 元",
        "",
        "## 事实与门禁",
        "",
    ]
    if blocks:
        lines.extend(f"- `{block}`" for block in blocks)
    else:
        lines.append("- 数据和风险门禁通过，可生成下一开盘虚拟订单。")
    lines.extend(["", "## 决策", "", "| 代码 | 名称 | 动作 | 目标权重 | 依据 |", "|---|---|---:|---:|---|"])
    for item in recommendations:
        lines.append(
            f"| {item['symbol']} | {item['name']} | {item['action']} | {_fmt_pct(item['target_weight'])} | {', '.join(item['reasons'])} |"
        )
    lines.extend(["", "## 虚拟成交与待执行订单", ""])
    if not fills and not orders:
        lines.append("- 无。休市、陈旧/不足数据或市场冲击会默认阻止交易。")
    for fill in fills:
        lines.append(f"- {fill['status']} {fill['side']} {fill['symbol']} {fill['quantity']} @ {fill.get('fill_price', 'UNKNOWN')}")
    for order in orders:
        lines.append(f"- PENDING {order['side']} {order['symbol']} {order['quantity']}，下一开盘价门限 {order['limit_price']}")
    lines.extend(["", "## 证据", ""])
    for evidence in snapshot["evidence"]:
        lines.append(f"- [{evidence['title']}]({evidence['url']}) — {evidence['as_of']}，{evidence['tier']}")
    infra = ledger["research_infrastructure"]
    lines.extend(
        [
            "",
            "## DeepSeek 使用审计",
            "",
            f"- 实际调用：{infra['actual_calls']} 次",
            f"- 实际记账成本：{infra['spent_cny']:.6f} 元",
            "- 密钥未写入仓库、报告或自动化提示。",
            "",
            "## 边界",
            "",
            "本报告仅用于本地虚拟组合实验，不构成真实投资建议，也不会连接券商或真实下单。",
            "",
        ]
    )
    return "\n".join(lines)


def run_pipeline(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
    mode: str = "short",
) -> dict[str, Any]:
    snapshot = _read_json(input_path)
    strategy = _read_json(strategy_path)
    warnings = validate_snapshot(snapshot, strategy)
    report_path = report_dir / f"{snapshot['run_date']}-{mode}.md"
    decision_path = report_dir / f"{snapshot['run_date']}-{mode}.json"
    if report_path.exists() or decision_path.exists():
        raise FileExistsError(f"不可覆盖既有运行产物: {report_path}")
    initialize_ledger(ledger_path)
    ledger = _read_json(ledger_path)
    fills = _settle_pending(ledger, snapshot, strategy)
    recommendations, blocks = _recommendations(ledger, snapshot, strategy, warnings)
    orders = _create_orders(ledger, snapshot, strategy, recommendations, blocks)
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets)
    input_hash = _sha256(input_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": mode,
        "input_sha256": input_hash,
        "as_of": snapshot["as_of"],
        "blocks": blocks,
        "recommendations": recommendations,
        "fills": fills,
        "new_orders": orders,
        "valuation": values,
    }
    ledger["last_run_id"] = run_id
    ledger["run_history"].append(
        {"run_id": run_id, "run_date": snapshot["run_date"], "mode": mode, "input_sha256": input_hash}
    )
    _atomic_json(ledger_path, ledger)
    _atomic_json(
        ledger_path.parent / "heartbeat.json",
        {
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": mode,
            "input_sha256": input_hash,
        },
    )
    for event in fills + orders:
        _append_jsonl(orders_log, {"run_id": run_id, **event})
    report_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(decision_path, decision)
    report_path.write_text(
        _render_report(snapshot, ledger, values, recommendations, blocks, fills, orders, input_hash, mode),
        encoding="utf-8",
    )
    return {
        "status": "PASS",
        "run_id": run_id,
        "report": str(report_path),
        "decision": str(decision_path),
        "blocks": blocks,
        "new_orders": len(orders),
        "fills": len(fills),
        "project_equity_cny": values["project_equity_cny"],
    }


def _settle_pending_fund_orders(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any]
) -> list[dict[str, Any]]:
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    precision = int(strategy.get("fund_orders", {}).get("share_precision", 4))
    events: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for order in ledger["pending_orders"]:
        if order.get("status") != "PENDING_NEXT_NAV":
            still_pending.append(order)
            continue
        asset = assets.get(str(order["symbol"]))
        if not asset:
            still_pending.append(order)
            continue
        metadata = asset.get("fund_metadata") or {}
        nav_date = str(metadata.get("nav_date", ""))
        if not nav_date or nav_date <= str(order["signal_date"]):
            still_pending.append(order)
            continue
        gate_reasons = _fund_gate_reasons(asset, strategy)
        if gate_reasons:
            order["status"] = "CANCELLED_FUND_DATA_GATE"
            order["cancellation_reason"] = ",".join(gate_reasons)
            events.append(order)
            continue
        nav = Decimal(str(asset["close"]))
        if order["side"] == "BUY":
            amount = _money(order["amount_cny"])
            if amount > _money(ledger["cash_cny"]):
                order["status"] = "CANCELLED_INSUFFICIENT_CASH"
                order["cancellation_reason"] = "INSUFFICIENT_CASH"
                events.append(order)
                continue
            fee_rate = Decimal(str(metadata["purchase_fee_rate"]))
            net_subscription = amount / (Decimal("1") + fee_rate)
            fee = _money(amount - net_subscription)
            shares = round(float(net_subscription / nav), precision)
            if shares <= 0:
                order["status"] = "CANCELLED_ZERO_SHARES"
                order["cancellation_reason"] = "ZERO_SHARES_AFTER_FEES"
                events.append(order)
                continue
            position = ledger["positions"].get(
                order["symbol"], {"quantity": 0.0, "average_cost": 0.0}
            )
            old_quantity = Decimal(str(position["quantity"]))
            old_cost = _money(Decimal(str(position["average_cost"])) * old_quantity)
            new_quantity = old_quantity + Decimal(str(shares))
            position.update(
                {
                    "quantity": float(new_quantity),
                    "average_cost": float((old_cost + amount) / new_quantity),
                    "name": asset["name"],
                    "asset_type": asset["asset_type"],
                    "risk_bucket": asset.get("risk_bucket", _default_risk_bucket(asset)),
                    "exposure_group": asset.get("exposure_group", order["symbol"]),
                    "acquired_date": position.get("acquired_date", nav_date),
                    "valuation_basis": "confirmed_nav",
                }
            )
            ledger["positions"][order["symbol"]] = position
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) - amount)
            order["confirmed_shares"] = shares
            order["gross_amount_cny"] = float(amount)
        else:
            position = ledger["positions"].get(order["symbol"])
            shares = Decimal(str(order.get("quantity", 0)))
            if not position or shares <= 0 or shares > Decimal(str(position["quantity"])):
                order["status"] = "CANCELLED_POSITION"
                order["cancellation_reason"] = "INSUFFICIENT_FUND_SHARES"
                events.append(order)
                continue
            gross = _money(nav * shares)
            fee_rate = Decimal(str(metadata["redemption_fee_rate"]))
            fee = _money(gross * fee_rate)
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) + gross - fee)
            remaining = Decimal(str(position["quantity"])) - shares
            if remaining == 0:
                del ledger["positions"][order["symbol"]]
            else:
                position["quantity"] = float(remaining)
            order["gross_amount_cny"] = float(gross)
            order["confirmed_shares"] = float(shares)
        order["status"] = "FILLED"
        order["fill_date"] = nav_date
        order["fill_as_of"] = snapshot["as_of"]
        order["fill_nav"] = float(nav)
        order["fund_fee_cny"] = float(fee)
        order["total_fees_cny"] = float(fee)
        order["settlement_kind"] = "CONFIRMED_NAV"
        events.append(order)
    ledger["pending_orders"] = still_pending
    return events


def _create_fund_orders(
    ledger: dict[str, Any],
    snapshot: dict[str, Any],
    strategy: dict[str, Any],
    recommendations: list[dict[str, Any]],
    blocks: list[str],
) -> list[dict[str, Any]]:
    hard_blocks = [block for block in blocks if block != "BROAD_MARKET_SHOCK"]
    if hard_blocks or not snapshot["is_trading_day"] or snapshot.get("market_state") != "nav_published":
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets)
    portfolio_value = float(values["investable_value_cny"])
    diversification = strategy.get("diversification", {})
    bucket_caps = diversification.get("bucket_weight_caps", {})
    max_positions = int(diversification.get("maximum_positions", 10**9))
    max_new_buys = int(diversification.get("maximum_new_buys_per_cycle", 10**9))
    minimum_cash_value = portfolio_value * float(strategy["risk"].get("minimum_cash_weight", 0))
    minimum_subscription = float(strategy.get("fund_orders", {}).get("minimum_subscription_cny", 10))
    used_symbols = set(ledger["positions"])
    used_groups = {
        str(position.get("exposure_group", symbol))
        for symbol, position in ledger["positions"].items()
    }
    bucket_values: dict[str, float] = {}
    equity_value = 0.0
    pending_buy_value = 0.0
    existing_pending = {
        str(order["symbol"])
        for order in ledger["pending_orders"]
        if order.get("status") in {"PENDING_NEXT_OPEN", "PENDING_NEXT_NAV"}
    }
    for symbol, position in ledger["positions"].items():
        asset = assets.get(symbol, position)
        value = float(assets.get(symbol, {}).get("close", position["average_cost"])) * float(position["quantity"])
        bucket = str(position.get("risk_bucket", asset.get("risk_bucket", _default_risk_bucket(asset))))
        bucket_values[bucket] = bucket_values.get(bucket, 0.0) + value
        if _is_equity_exposure(asset):
            equity_value += value
    for order in ledger["pending_orders"]:
        if order.get("side") != "BUY" or order.get("status") not in {
            "PENDING_NEXT_OPEN",
            "PENDING_NEXT_NAV",
        }:
            continue
        if order["status"] == "PENDING_NEXT_NAV":
            value = float(order["amount_cny"])
        else:
            value = float(order["signal_close"]) * int(order["quantity"])
        bucket = str(order.get("risk_bucket", "unclassified_equity"))
        bucket_values[bucket] = bucket_values.get(bucket, 0.0) + value
        pending_buy_value += value
        used_symbols.add(str(order["symbol"]))
        used_groups.add(str(order.get("exposure_group", order["symbol"])))
        if str(order.get("asset_type")) in EQUITY_EXPOSURE_TYPES:
            equity_value += value

    orders: list[dict[str, Any]] = []
    buy_count = sum(
        order.get("side") == "BUY"
        and order.get("status") in {"PENDING_NEXT_OPEN", "PENDING_NEXT_NAV"}
        and str(order.get("signal_date")) == str(snapshot["run_date"])
        for order in ledger["pending_orders"]
    )

    def block_item(item: dict[str, Any], reason: str) -> None:
        item["action"] = "WATCH"
        item["target_weight"] = 0.0
        if reason not in item["reasons"]:
            item["reasons"].append(reason)

    for recommendation in recommendations:
        symbol = str(recommendation["symbol"])
        asset = assets[symbol]
        if str(asset.get("asset_type")) not in OPEN_END_FUND_TYPES:
            continue
        if symbol in existing_pending or recommendation["action"] not in ("BUY", "SELL"):
            continue
        bucket = str(asset.get("risk_bucket", _default_risk_bucket(asset)))
        group = str(asset.get("exposure_group", symbol))
        if recommendation["action"] == "BUY":
            if buy_count >= max_new_buys:
                block_item(recommendation, "MAXIMUM_NEW_BUYS_REACHED")
                continue
            if len(used_symbols) >= max_positions:
                block_item(recommendation, "MAXIMUM_POSITIONS_REACHED")
                continue
            if diversification.get("one_position_per_exposure_group", False) and group in used_groups:
                block_item(recommendation, "DUPLICATE_EXPOSURE_GROUP")
                continue
            amount = portfolio_value * float(recommendation["target_weight"])
            amount = min(
                amount,
                max(0.0, portfolio_value * float(bucket_caps.get(bucket, 1.0)) - bucket_values.get(bucket, 0.0)),
            )
            if _is_equity_exposure(asset):
                amount = min(
                    amount,
                    max(0.0, portfolio_value * float(strategy["risk"]["max_total_equity_weight"]) - equity_value),
                )
            amount = _money(
                min(
                    amount,
                    max(0.0, float(ledger["cash_cny"]) - pending_buy_value - minimum_cash_value),
                )
            )
            if amount < _money(minimum_subscription):
                block_item(recommendation, "FUND_SUBSCRIPTION_TOO_SMALL_AFTER_DIVERSIFICATION")
                continue
            side = "BUY"
            buy_count += 1
            pending_buy_value += float(amount)
            bucket_values[bucket] = bucket_values.get(bucket, 0.0) + float(amount)
            if _is_equity_exposure(asset):
                equity_value += float(amount)
            used_symbols.add(symbol)
            used_groups.add(group)
            recommendation["target_weight"] = float(amount) / portfolio_value if portfolio_value else 0.0
            order_value: dict[str, Any] = {"amount_cny": float(amount)}
        else:
            position = ledger["positions"].get(symbol)
            if not position:
                continue
            side = "SELL"
            order_value = {"quantity": float(position["quantity"])}
        metadata = asset["fund_metadata"]
        order = {
            "order_id": uuid.uuid4().hex,
            "status": "PENDING_NEXT_NAV",
            "side": side,
            "symbol": symbol,
            "name": asset["name"],
            "asset_type": asset["asset_type"],
            "risk_bucket": bucket,
            "exposure_group": group,
            "signal_date": snapshot["run_date"],
            "signal_as_of": snapshot["as_of"],
            "signal_nav": float(asset["close"]),
            "pricing_rule": "NEXT_OPEN_DAY_CONFIRMED_NAV",
            "purchase_fee_rate": float(metadata["purchase_fee_rate"]),
            "redemption_fee_rate": float(metadata["redemption_fee_rate"]),
            "simulation_only": True,
            "reasons": recommendation["reasons"],
            **order_value,
        }
        ledger["pending_orders"].append(order)
        orders.append(order)
    return orders


def _render_fund_report(
    snapshot: dict[str, Any],
    values: dict[str, float],
    recommendations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    blocks: list[str],
    input_hash: str,
) -> str:
    lines = [
        f"# Vibe Finance {snapshot['run_date']} 基金净值晚间报告",
        "",
        f"- 证据截点：{snapshot['as_of']}",
        f"- 输入 SHA-256：`{input_hash}`",
        f"- 项目权益：{values['project_equity_cny']:.2f} 元",
        f"- 已确认净值成交：{sum(event.get('status') == 'FILLED' for event in events)}",
        f"- 新增待确认净值订单：{len(orders)}",
        "",
        "## 门禁与决策",
        "",
    ]
    lines.extend(f"- `{block}`" for block in blocks)
    lines.extend(["", "| 代码 | 名称 | 动作 | 目标权重 | 依据 |", "|---|---|---:|---:|---|"])
    for item in recommendations:
        if item["asset_type"] in OPEN_END_FUND_TYPES:
            lines.append(
                f"| {item['symbol']} | {item['name']} | {item['action']} | "
                f"{_fmt_pct(item['target_weight'])} | {', '.join(item['reasons'])} |"
            )
    lines.extend(["", "## 净值确认事件", ""])
    if not events and not orders:
        lines.append("- 无。")
    for event in events:
        lines.append(
            f"- {event['status']} {event['side']} {event['symbol']}，确认净值 "
            f"{event.get('fill_nav', 'UNKNOWN')}，费用 {event.get('total_fees_cny', 0):.2f} 元。"
        )
    for order in orders:
        size = order.get("amount_cny", order.get("quantity"))
        lines.append(f"- PENDING_NEXT_NAV {order['side']} {order['symbol']}，申请规模 {size}。")
    lines.extend(["", "## 证据", ""])
    for evidence in snapshot["evidence"]:
        lines.append(f"- [{evidence['title']}]({evidence['url']}) — {evidence['as_of']}，{evidence['tier']}")
    lines.extend(
        [
            "",
            "场外基金遵循未知价原则：15:00 后信号按下一开放日已确认净值加减费用模拟，不能回填当日已知净值。",
            "",
        ]
    )
    return "\n".join(lines)


def run_fund_nav_pipeline(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_FUND_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
) -> dict[str, Any]:
    snapshot = _read_json(input_path)
    strategy = _read_json(strategy_path)
    warnings = validate_snapshot(snapshot, strategy)
    report_path = report_dir / f"{snapshot['run_date']}-funds.md"
    decision_path = report_dir / f"{snapshot['run_date']}-funds.json"
    if report_path.exists() or decision_path.exists():
        raise FileExistsError(f"不可覆盖既有基金净值产物: {report_path}")
    initialize_ledger(ledger_path)
    ledger = _read_json(ledger_path)
    blocks = list(warnings)
    if not snapshot["is_trading_day"]:
        blocks.append("NON_TRADING_DAY")
    if snapshot.get("market_state") != "nav_published":
        blocks.append("NAV_NOT_PUBLISHED")
    events: list[dict[str, Any]] = []
    if not [block for block in blocks if block != "BROAD_MARKET_SHOCK"]:
        events = _settle_pending_fund_orders(ledger, snapshot, strategy)
    recommendations, recommendation_blocks = _recommendations(ledger, snapshot, strategy, warnings)
    blocks = sorted(set(blocks + recommendation_blocks))
    orders = _create_fund_orders(ledger, snapshot, strategy, recommendations, blocks)
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets)
    input_hash = _sha256(input_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": "fund_nav",
        "input_sha256": input_hash,
        "as_of": snapshot["as_of"],
        "blocks": blocks,
        "recommendations": recommendations,
        "events": events,
        "new_orders": orders,
        "valuation": values,
    }
    ledger["last_run_id"] = run_id
    ledger["run_history"].append(
        {"run_id": run_id, "run_date": snapshot["run_date"], "mode": "fund_nav", "input_sha256": input_hash}
    )
    _atomic_json(ledger_path, ledger)
    _atomic_json(
        ledger_path.parent / "heartbeat.json",
        {
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": "fund_nav",
            "input_sha256": input_hash,
        },
    )
    for event in events + orders:
        _append_jsonl(orders_log, {"run_id": run_id, **event})
    report_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(decision_path, decision)
    report_path.write_text(
        _render_fund_report(snapshot, values, recommendations, events, orders, blocks, input_hash),
        encoding="utf-8",
    )
    return {
        "status": "PASS",
        "run_id": run_id,
        "report": str(report_path),
        "decision": str(decision_path),
        "filled": sum(event.get("status") == "FILLED" for event in events),
        "new_orders": len(orders),
        "pending_after": sum(
            order.get("status") == "PENDING_NEXT_NAV" for order in ledger["pending_orders"]
        ),
        "project_equity_cny": values["project_equity_cny"],
    }


def _render_execution_report(
    snapshot: dict[str, Any],
    values: dict[str, float],
    events: list[dict[str, Any]],
    blocks: list[str],
    input_hash: str,
    pending_before: int,
    pending_after: int,
    strategy: dict[str, Any],
) -> str:
    filled = sum(event.get("status") == "FILLED" for event in events)
    cancelled = sum(str(event.get("status", "")).startswith("CANCELLED") for event in events)
    status = "BLOCKED" if blocks else ("FILLED" if filled else "NO_PENDING_OR_FILL")
    costs = strategy["costs"]
    lines = [
        f"# Vibe Finance {snapshot['run_date']} 开盘虚拟成交结算",
        "",
        f"- 状态：`{status}`",
        f"- 证据截点：{snapshot['as_of']}",
        f"- 输入 SHA-256：`{input_hash}`",
        f"- 结算前待执行：{pending_before}",
        f"- 成交：{filled}；取消：{cancelled}；结算后待执行：{pending_after}",
        f"- 估算项目权益：{values['project_equity_cny']:.2f} 元",
        "",
        "## 结算门禁",
        "",
    ]
    if blocks:
        lines.extend(f"- `{block}`" for block in blocks)
    else:
        lines.append("- 交易日、开盘状态及双源开盘价门禁通过。")
    lines.extend(["", "## 虚拟成交事件", ""])
    if not events:
        lines.append("- 无待执行订单，或本次结算被门禁阻止。")
    for event in events:
        if event.get("status") == "FILLED":
            lines.append(
                f"- FILLED {event['side']} {event['symbol']} {event['quantity']} @ "
                f"{event['fill_price']}；佣金 {event['commission_cny']:.2f} 元，"
                f"过户费 {event['transfer_fee_cny']:.2f} 元，"
                f"印花税 {event['stamp_tax_cny']:.2f} 元，"
                f"合计 {event['total_fees_cny']:.2f} 元。"
            )
        else:
            lines.append(
                f"- {event['status']} {event['side']} {event['symbol']} "
                f"{event['quantity']}；{event.get('cancellation_reason', '门限或资金不满足')}。"
            )
    lines.extend(
        [
            "",
            "## 费用模型",
            "",
            f"- 券商佣金假设：成交额 {float(costs['commission_rate']) * 100:.3f}%，"
            f"单笔最低 {float(costs['minimum_commission_cny']):.2f} 元，买卖双向。",
            f"- A 股过户费：成交额 {float(costs.get('stock_transfer_fee_rate', 0)) * 100:.3f}%，买卖双向。",
            f"- A 股印花税：成交额 {float(costs['stock_sell_stamp_rate']) * 100:.3f}%，仅卖出。",
            "- 场内 ETF：本模型只收券商佣金，不收股票过户费或证券交易印花税。",
            "- 券商佣金按账户协议可能不同；交易所经手费视为包含在佣金中，不重复计费。",
            "",
            "本次只结算上一收盘已生成的虚拟订单，不产生新信号，不连接券商。",
            "",
        ]
    )
    return "\n".join(lines)


def settle_open_orders(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_EXECUTION_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
) -> dict[str, Any]:
    """Settle prior-close virtual orders at a later, double-sourced opening price."""
    snapshot = _read_json(input_path)
    strategy = _read_json(strategy_path)
    warnings = validate_snapshot(snapshot, strategy)
    report_path = report_dir / f"{snapshot['run_date']}-open.md"
    decision_path = report_dir / f"{snapshot['run_date']}-open.json"
    if report_path.exists() or decision_path.exists():
        raise FileExistsError(f"不可覆盖既有开盘结算产物: {report_path}")

    initialize_ledger(ledger_path)
    ledger = _read_json(ledger_path)
    pending_before = sum(
        order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
    )
    blocks = list(warnings)
    if not snapshot["is_trading_day"]:
        blocks.append("NON_TRADING_DAY")
    if snapshot.get("market_state") not in {"open", "opening_auction_complete"}:
        blocks.append("MARKET_NOT_OPEN")
    events: list[dict[str, Any]] = []
    if not blocks:
        events = _settle_pending(
            ledger,
            snapshot,
            strategy,
            allowed_market_states={"open", "opening_auction_complete"},
            require_open_source_ids=True,
        )

    valuation_assets: dict[str, dict[str, Any]] = {}
    for item in snapshot["assets"]:
        valued = dict(item)
        if valued.get("open") is not None:
            valued["close"] = valued["open"]
        valuation_assets[str(valued["symbol"])] = valued
    values = _project_value(ledger, valuation_assets)
    input_hash = _sha256(input_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": "open_settlement",
        "input_sha256": input_hash,
        "as_of": snapshot["as_of"],
        "blocks": sorted(set(blocks)),
        "events": events,
        "pending_before": pending_before,
        "pending_after": sum(
            order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
        ),
        "valuation": values,
    }
    ledger["last_run_id"] = run_id
    ledger["run_history"].append(
        {
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": "open_settlement",
            "input_sha256": input_hash,
        }
    )
    _atomic_json(ledger_path, ledger)
    _atomic_json(
        ledger_path.parent / "heartbeat.json",
        {
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": "open_settlement",
            "input_sha256": input_hash,
        },
    )
    for event in events:
        _append_jsonl(orders_log, {"run_id": run_id, **event})
    report_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(decision_path, decision)
    report_path.write_text(
        _render_execution_report(
            snapshot,
            values,
            events,
            sorted(set(blocks)),
            input_hash,
            pending_before,
            sum(
                order.get("status") == "PENDING_NEXT_OPEN"
                for order in ledger["pending_orders"]
            ),
            strategy,
        ),
        encoding="utf-8",
    )
    return {
        "status": "PASS",
        "run_id": run_id,
        "report": str(report_path),
        "decision": str(decision_path),
        "blocks": sorted(set(blocks)),
        "filled": sum(event.get("status") == "FILLED" for event in events),
        "cancelled": sum(
            str(event.get("status", "")).startswith("CANCELLED")
            for event in events
        ),
        "pending_after": sum(
            order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
        ),
        "project_equity_cny": values["project_equity_cny"],
    }


def project_status(
    ledger_path: Path = DEFAULT_LEDGER,
    report_dir: Path = DEFAULT_REPORT_DIR,
    max_age_hours: float = 36.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only liveness and budget status; never refreshes the heartbeat."""
    heartbeat_path = ledger_path.parent / "heartbeat.json"
    if not ledger_path.exists() or not heartbeat_path.exists():
        return {"status": "INACTIVE", "reason": "LEDGER_OR_HEARTBEAT_MISSING"}
    ledger = _read_json(ledger_path)
    heartbeat = _read_json(heartbeat_path)
    last_success = _parse_time(str(heartbeat["last_success_at"]))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_hours = (current.astimezone(last_success.tzinfo) - last_success).total_seconds() / 3600
    reports = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    infra = ledger["research_infrastructure"]
    remaining = _money(infra["reserved_cny"]) - _money(infra["spent_cny"])
    return {
        "status": "ACTIVE" if age_hours <= max_age_hours else "STALE",
        "heartbeat_age_hours": round(max(0.0, age_hours), 3),
        "max_age_hours": max_age_hours,
        "last_success_at": heartbeat["last_success_at"],
        "last_run_id": heartbeat["run_id"],
        "last_run_date": heartbeat["run_date"],
        "last_mode": heartbeat["mode"],
        "decision_report_count": len(reports),
        "pending_orders": len(ledger["pending_orders"]),
        "positions": len(ledger["positions"]),
        "deepseek_actual_calls": int(infra["actual_calls"]),
        "deepseek_spent_cny": float(_money(infra["spent_cny"])),
        "deepseek_remaining_cny": float(remaining),
    }


def record_api_cost(
    ledger_path: Path,
    cost_log: Path,
    amount_cny: float,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    if amount_cny <= 0 or input_tokens < 0 or output_tokens < 0:
        raise ValueError("成本必须为正，token 数不得为负")
    ledger = _read_json(ledger_path)
    infra = ledger["research_infrastructure"]
    new_spent = _money(infra["spent_cny"]) + _money(amount_cny)
    if new_spent > _money(infra["reserved_cny"]):
        raise ValueError("DeepSeek 调用将超过 100 元研究预算")
    infra["spent_cny"] = float(new_spent)
    infra["actual_calls"] = int(infra["actual_calls"]) + 1
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "DeepSeek",
        "model": model,
        "purpose": purpose,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "amount_cny": float(_money(amount_cny)),
        "actual_api_call": True,
    }
    _atomic_json(ledger_path, ledger)
    _append_jsonl(cost_log, event)
    return {
        "status": "RECORDED",
        "actual_calls": infra["actual_calls"],
        "spent_cny": infra["spent_cny"],
        "remaining_cny": float(_money(infra["reserved_cny"]) - new_spent),
    }
