from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import pstdev
from typing import Any

from .evolution import (
    REPO_ROOT as EVOLUTION_REPO_ROOT,
    pipeline_event_payload_sha256,
)
from .transaction import (
    inspect_transaction_state,
    locked_state,
    prepare_run_transaction,
    recover_incomplete_transactions,
)


DEFAULT_LEDGER = Path("data/ledger/portfolio.json")
DEFAULT_ORDERS_LOG = Path("data/ledger/orders.jsonl")
DEFAULT_REPORT_DIR = Path("reports/daily")
DEFAULT_EXECUTION_REPORT_DIR = Path("reports/execution")
DEFAULT_FUND_REPORT_DIR = Path("reports/funds")
DEFAULT_STRATEGY = Path("config/strategy.json")
DEFAULT_README = Path("README.md")
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
ACTIONABLE_BUY_ACTIONS = {"BUY", "ADD"}
ACTIONABLE_SELL_ACTIONS = {"SELL", "REDUCE"}
LISTED_ASSET_TYPES = {"stock", "equity_etf", "cash_etf", "bond_etf", "gold_etf"}


class DataGateError(ValueError):
    """Raised when point-in-time data is unsafe for decision generation."""


def _money(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


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
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _repo_relative_artifact(path: Path, repo_root: Path) -> str | None:
    try:
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return None
    if path.exists() and path.is_symlink():
        return None
    return relative.as_posix()


def _prepare_pipeline_events(
    *,
    events: list[dict[str, Any]],
    run_id: str,
    mode: str,
    input_path: Path,
    strategy_path: Path,
    decision_path: Path,
    decision: dict[str, Any],
    repo_root: Path = EVOLUTION_REPO_ROOT,
) -> list[dict[str, Any]]:
    input_relative = _repo_relative_artifact(input_path, repo_root)
    strategy_relative = _repo_relative_artifact(strategy_path, repo_root)
    decision_relative = _repo_relative_artifact(decision_path, repo_root)
    artifact_bound = all(
        value is not None
        for value in (input_relative, strategy_relative, decision_relative)
    )
    appended: list[dict[str, Any]] = []
    decision_payload = (
        json.dumps(
            decision,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    for event in events:
        payload = {"run_id": run_id, **event}
        if artifact_bound:
            provenance = {
                "schema_version": 2,
                "producer": "vibe_finance.pipeline",
                "pipeline_mode": mode,
                "input_path": input_relative,
                "input_sha256": _sha256(input_path),
                "strategy_path": strategy_relative,
                "strategy_sha256": _sha256(strategy_path),
                "decision_manifest_path": decision_relative,
                "decision_manifest_sha256": hashlib.sha256(decision_payload).hexdigest(),
            }
            provenance["event_payload_sha256"] = pipeline_event_payload_sha256(payload)
            payload["evolution_provenance"] = provenance
        appended.append(payload)
    return appended


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
        "performance": {
            "filled_trade_count": 0,
            "cumulative_buy_notional_cny": 0.0,
            "cumulative_sell_notional_cny": 0.0,
            "cumulative_fees_cny": 0.0,
            "realized_pnl_cny": 0.0,
            "last_filled_trade_date": None,
            "last_valuation_as_of": None,
            "investable_value_cny": 29900.0,
            "project_equity_cny": 30000.0,
            "total_pnl_cny": 0.0,
            "total_return_pct": 0.0,
        },
        "last_run_id": None,
        "run_history": [],
    }
    _atomic_json(path, ledger)
    return {"status": "CREATED", "ledger": str(path)}


def _ensure_ledger_schema(ledger: dict[str, Any]) -> None:
    performance = ledger.setdefault("performance", {})
    defaults = {
        "filled_trade_count": 0,
        "cumulative_buy_notional_cny": 0.0,
        "cumulative_sell_notional_cny": 0.0,
        "cumulative_fees_cny": 0.0,
        "realized_pnl_cny": 0.0,
        "last_filled_trade_date": None,
        "last_valuation_as_of": None,
        "investable_value_cny": float(ledger.get("cash_cny", 0.0)),
        "project_equity_cny": float(ledger.get("initial_project_capital_cny", 0.0)),
        "total_pnl_cny": 0.0,
        "total_return_pct": 0.0,
    }
    for key, value in defaults.items():
        performance.setdefault(key, value)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataGateError(f"无效 ISO 时间: {value}") from exc
    if parsed.tzinfo is None:
        raise DataGateError("as_of 必须含时区")
    return parsed


def _parse_run_date(value: Any) -> date:
    if not isinstance(value, str):
        raise DataGateError("run_date 必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataGateError("run_date 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise DataGateError("run_date 必须是 YYYY-MM-DD")
    return parsed


def _validate_observed_at(
    value: Any,
    cutoff: datetime,
    label: str,
    *,
    allow_date_only: bool = False,
) -> None:
    if allow_date_only and isinstance(value, str):
        try:
            observed_date = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if observed_date > cutoff.date():
                raise DataGateError(f"{label} 位于快照截点之后")
            return
    try:
        observed = _parse_time(str(value))
    except (DataGateError, TypeError) as exc:
        raise DataGateError(f"{label} 必须是带时区 ISO 时间") from exc
    if observed > cutoff.astimezone(observed.tzinfo):
        raise DataGateError(f"{label} 位于快照截点之后")


def _price_source_ids(asset: dict[str, Any]) -> set[str]:
    values = asset.get("price_source_ids", [])
    if not isinstance(values, list):
        raise DataGateError(f"{asset.get('symbol', '')} price_source_ids 必须是 list")
    return {str(value) for value in values if str(value)}


def validate_snapshot(
    snapshot: dict[str, Any], strategy: dict[str, Any], now: datetime | None = None
) -> list[str]:
    required = ("schema_version", "run_date", "as_of", "is_trading_day", "indices", "assets", "evidence")
    missing = [key for key in required if key not in snapshot]
    if missing:
        raise DataGateError(f"快照缺少字段: {', '.join(missing)}")
    if snapshot["schema_version"] not in {1, 2}:
        raise DataGateError("不支持的快照 schema_version")
    run_date = _parse_run_date(snapshot["run_date"])
    as_of = _parse_time(snapshot["as_of"])
    if as_of.date() > run_date:
        raise DataGateError("as_of 日期不得晚于 run_date")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if as_of > current.astimezone(as_of.tzinfo):
        raise DataGateError("as_of 位于未来，触发前视偏差门禁")

    collection_rules = strategy.get("data_collection", {})
    coverage_effective_raw = collection_rules.get(
        "daily_snapshot_coverage_effective_date"
    )
    required_daily_types = {
        str(value)
        for value in collection_rules.get("daily_snapshot_asset_types", [])
        if str(value)
    }
    if (
        snapshot["is_trading_day"]
        and coverage_effective_raw
        and run_date >= _parse_run_date(str(coverage_effective_raw))
        and required_daily_types
    ):
        present_daily_types = {
            str(asset.get("asset_type")) for asset in snapshot["assets"]
        }
        missing_daily_types = sorted(required_daily_types - present_daily_types)
        if missing_daily_types:
            raise DataGateError(
                "交易日快照缺少必需资产类型: " + ", ".join(missing_daily_types)
            )

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
        sources = asset.get("source_ids")
        if not isinstance(sources, list):
            raise DataGateError(f"{symbol} source_ids 必须是 list")
        price_as_of = asset.get("price_as_of")
        price_sources = _price_source_ids(asset)
        if snapshot["schema_version"] == 2:
            if not price_as_of:
                raise DataGateError(f"{symbol} schema v2 缺少 price_as_of")
            if not price_sources:
                raise DataGateError(f"{symbol} schema v2 缺少 price_source_ids")
        else:
            if not price_as_of:
                warnings.append(f"LEGACY_IMPLICIT_PRICE_AS_OF:{symbol}")
            if not price_sources:
                warnings.append(f"LEGACY_UNSCOPED_PRICE_SOURCES:{symbol}")
        if price_as_of:
            _validate_observed_at(price_as_of, as_of, f"{symbol} price_as_of")
        if not price_sources.issubset({str(value) for value in sources}):
            raise DataGateError(f"{symbol} price_source_ids 必须是 source_ids 的子集")
        if snapshot["schema_version"] == 2 and len(price_sources) < int(
            strategy["data_gates"]["minimum_price_sources"]
        ):
            raise DataGateError(f"{symbol} price_source_ids 未达到双源门禁")
        if asset.get("open") is not None:
            open_as_of = asset.get("open_price_as_of")
            open_sources = asset.get("open_source_ids")
            if snapshot["schema_version"] == 2 and not open_as_of:
                raise DataGateError(f"{symbol} schema v2 缺少 open_price_as_of")
            if open_as_of:
                _validate_observed_at(open_as_of, as_of, f"{symbol} open_price_as_of")
            if snapshot["schema_version"] == 2 and (
                not isinstance(open_sources, list)
                or len({str(value) for value in open_sources if str(value)})
                < int(strategy["data_gates"]["minimum_price_sources"])
            ):
                raise DataGateError(f"{symbol} open_source_ids 未达到双源门禁")
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
    if not isinstance(snapshot["evidence"], list):
        raise DataGateError("evidence 必须是 list")
    for index, evidence in enumerate(snapshot["evidence"], 1):
        if not isinstance(evidence, dict) or not evidence.get("as_of"):
            raise DataGateError(f"evidence[{index}] 缺少 as_of")
        _validate_observed_at(
            evidence["as_of"],
            as_of,
            f"evidence[{index}].as_of",
            allow_date_only=True,
        )
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


def _project_value(
    ledger: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    valuation_as_of: str | None = None,
) -> dict[str, Any]:
    positions_value = Decimal("0")
    position_marks: dict[str, dict[str, Any]] = {}
    stale_symbols: list[str] = []
    mark_blocks: list[str] = []
    for symbol, position in ledger["positions"].items():
        asset = assets.get(symbol)
        asset_price_sources = (
            asset.get("price_source_ids", []) if asset is not None else []
        )
        asset_is_trusted = (
            asset is not None
            and bool(asset.get("price_as_of"))
            and isinstance(asset_price_sources, list)
            and len({str(value) for value in asset_price_sources if str(value)}) >= 2
        )
        if asset_is_trusted:
            price = Decimal(str(asset["close"]))
            price_as_of = str(asset["price_as_of"])
            basis = "SNAPSHOT_PRICE"
        elif position.get("last_price") is not None and position.get("last_price_as_of"):
            price = Decimal(str(position["last_price"]))
            price_as_of = str(position["last_price_as_of"])
            basis = "LEDGER_LAST_PRICE"
            stale_symbols.append(str(symbol))
            mark_blocks.append(
                (
                    f"POSITION_MARK_UNTRUSTED:{symbol}"
                    if asset is not None
                    else f"POSITION_MARK_NOT_IN_SNAPSHOT:{symbol}"
                )
            )
        else:
            reason = "UNTRUSTED" if asset is not None else "MISSING"
            raise DataGateError(f"{reason}_POSITION_MARK:{symbol}")
        quantity = Decimal(str(position["quantity"]))
        if not quantity.is_finite() or quantity < 0:
            raise DataGateError(f"INVALID_POSITION_QUANTITY:{symbol}")
        market_value = _money(price * quantity)
        positions_value += market_value
        position_marks[str(symbol)] = {
            "price": str(price),
            "quantity": str(quantity),
            "market_value_cny": float(market_value),
            "price_as_of": price_as_of,
            "basis": basis,
        }
    cash = _money(ledger["cash_cny"])
    infra = ledger["research_infrastructure"]
    infrastructure_remaining = _money(infra["reserved_cny"]) - _money(infra["spent_cny"])
    return {
        "cash_cny": float(cash),
        "positions_cny": float(positions_value),
        "investable_value_cny": float(cash + positions_value),
        "infrastructure_remaining_cny": float(infrastructure_remaining),
        "project_equity_cny": float(cash + positions_value + infrastructure_remaining),
        "mark_status": "STALE" if stale_symbols else "COMPLETE",
        "stale_symbols": stale_symbols,
        "blocks": mark_blocks,
        "position_marks": position_marks,
    }


def _update_ledger_valuation(
    ledger: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    values: dict[str, Any],
    as_of: str,
) -> None:
    _ensure_ledger_schema(ledger)
    for symbol, position in ledger["positions"].items():
        mark = values["position_marks"][str(symbol)]
        price = Decimal(str(mark["price"]))
        quantity = Decimal(str(mark["quantity"]))
        if mark["basis"] == "SNAPSHOT_PRICE":
            position["last_price"] = float(price)
            position["last_price_as_of"] = str(mark["price_as_of"])
        position["market_value_cny"] = float(_money(price * quantity))
        average_cost = Decimal(str(position.get("average_cost", price)))
        position["unrealized_pnl_cny"] = float(_money((price - average_cost) * quantity))
    performance = ledger["performance"]
    initial = float(ledger.get("initial_project_capital_cny", 0.0))
    project_equity = float(values["project_equity_cny"])
    total_pnl = project_equity - initial
    valuation_complete = values.get("mark_status") == "COMPLETE"
    performance["valuation_complete"] = valuation_complete
    performance["valuation_stale_symbols"] = list(values.get("stale_symbols", []))
    if valuation_complete:
        performance["last_valuation_as_of"] = as_of
    else:
        performance["last_partial_valuation_as_of"] = as_of
    performance["investable_value_cny"] = float(values["investable_value_cny"])
    performance["project_equity_cny"] = project_equity
    performance["total_pnl_cny"] = float(_money(total_pnl))
    performance["total_return_pct"] = total_pnl / initial if initial else 0.0


def _record_filled_trade(
    ledger: dict[str, Any],
    *,
    side: str,
    notional: Decimal,
    fees: Decimal,
    fill_date: str,
    realized_pnl: Decimal = Decimal("0"),
) -> None:
    _ensure_ledger_schema(ledger)
    performance = ledger["performance"]
    performance["filled_trade_count"] = int(performance["filled_trade_count"]) + 1
    key = "cumulative_buy_notional_cny" if side == "BUY" else "cumulative_sell_notional_cny"
    performance[key] = float(_money(performance[key]) + notional)
    performance["cumulative_fees_cny"] = float(
        _money(performance["cumulative_fees_cny"]) + fees
    )
    performance["realized_pnl_cny"] = float(
        _money(performance["realized_pnl_cny"]) + realized_pnl
    )
    performance["last_filled_trade_date"] = fill_date


def _maximum_asset_weight(asset_type: str, strategy: dict[str, Any]) -> float:
    risk = strategy["risk"]
    if asset_type == "stock":
        return float(risk["max_single_stock_weight"])
    if asset_type == "cash_etf":
        return float(risk["cash_etf_target_weight"])
    if asset_type == "bond_etf":
        return float(risk["bond_fund_target_weight"])
    if asset_type == "gold_etf":
        return float(risk["gold_fund_target_weight"])
    if asset_type in OPEN_END_FUND_TYPES:
        return float(risk["max_single_open_end_fund_weight"])
    return float(risk["max_single_etf_weight"])


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
    snapshot_as_of = _parse_time(str(snapshot["as_of"]))
    _ensure_ledger_schema(ledger)
    events: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for order in ledger["pending_orders"]:
        if order.get("status") != "PENDING_NEXT_OPEN":
            still_pending.append(order)
            continue
        asset = assets.get(order["symbol"])
        signal_as_of = order.get("signal_as_of")
        if signal_as_of:
            if _parse_time(str(signal_as_of)) >= snapshot_as_of:
                still_pending.append(order)
                continue
        elif snapshot["run_date"] <= order["signal_date"]:
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
            position.setdefault("acquired_date", snapshot["run_date"])
            position["last_buy_date"] = snapshot["run_date"]
            ledger["positions"][order["symbol"]] = position
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) - total)
            realized_pnl = Decimal("0")
        else:
            position = ledger["positions"].get(order["symbol"])
            if not position or quantity > int(position["quantity"]):
                order["status"] = "CANCELLED_POSITION"
                order["cancellation_reason"] = "INSUFFICIENT_POSITION"
                events.append(order)
                continue
            cost_basis = _money(position["average_cost"] * quantity)
            realized_pnl = notional - fees["total_fees_cny"] - cost_basis
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
        order["realized_pnl_cny"] = float(realized_pnl)
        order["fee_model_version"] = str(strategy["version"])
        _record_filled_trade(
            ledger,
            side=str(order["side"]),
            notional=notional,
            fees=fees["total_fees_cny"],
            fill_date=str(snapshot["run_date"]),
            realized_pnl=realized_pnl,
        )
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


def _signal_target_weight(
    strategy: dict[str, Any], signal_name: str, risk_bucket: str, default: float
) -> float:
    signal = strategy.get("signals", {}).get(signal_name, {})
    return float(signal.get("target_weights", {}).get(risk_bucket, default))


def _recommendations(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any], warnings: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    _ensure_ledger_schema(ledger)
    shock = _market_shock(snapshot, strategy)
    global_blocks = list(warnings)
    if not snapshot["is_trading_day"]:
        global_blocks.append("NON_TRADING_DAY")
    if shock:
        global_blocks.append("BROAD_MARKET_SHOCK")

    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets, str(snapshot["as_of"]))
    global_blocks.extend(values["blocks"])
    portfolio_value = max(float(values["investable_value_cny"]), 1.0)
    signals = strategy.get("signals", {})
    cold_start_rules = signals.get("cold_start", {})
    cold_start_types = set(cold_start_rules.get("eligible_asset_types", []))
    cold_start_enabled = bool(cold_start_rules.get("enabled", False))
    dip_rules = signals.get("controlled_dip", {})
    trend_rules = signals.get("trend", {})
    defensive_rules = signals.get("defensive", {})
    daily_rules = strategy.get("daily_execution", {})
    conditional_preopen_types = set(daily_rules.get("eligible_asset_types", []))
    min_sources = int(strategy["data_gates"]["minimum_price_sources"])
    min_history = int(strategy["data_gates"]["minimum_history_points"])

    hard_reason_names = {
        "PRICE_NOT_CROSSCHECKED",
        "INSUFFICIENT_HISTORY",
        "DAILY_RETURN_MISSING",
        "NOT_TRADING",
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
    }
    recommendations: list[dict[str, Any]] = []

    for asset in snapshot["assets"]:
        symbol = str(asset["symbol"])
        asset_type = str(asset["asset_type"])
        bucket = str(asset.get("risk_bucket", _default_risk_bucket(asset)))
        metrics = _metrics(asset)
        history_points = len(asset.get("history", []))
        full_history = metrics is not None and history_points >= min_history
        cold_start = (
            cold_start_enabled
            and asset_type in cold_start_types
            and asset_type not in OPEN_END_FUND_TYPES
            and asset.get("daily_return") is not None
        )
        reasons: list[str] = _fund_gate_reasons(asset, strategy)
        action = "HOLD" if symbol in ledger["positions"] else "WATCH"
        target_weight = 0.0
        score = 0.0
        signal_type = "NONE"
        current_quantity = Decimal(
            str(ledger["positions"].get(symbol, {}).get("quantity", 0))
        )
        current_weight = float(Decimal(str(asset["close"])) * current_quantity) / portfolio_value

        price_sources = _price_source_ids(asset)
        if len(price_sources) < min_sources:
            reasons.append("PRICE_NOT_CROSSCHECKED")
        if not full_history:
            if cold_start:
                reasons.append("COLD_START_LIMITED_HISTORY")
            else:
                reasons.append("INSUFFICIENT_HISTORY")
        if asset.get("daily_return") is None and cold_start:
            reasons.append("DAILY_RETURN_MISSING")
        trading_status = str(asset.get("trading_status", "TRADING"))
        conditional_preopen_status = (
            snapshot.get("market_state") == "preopen"
            and bool(daily_rules.get("allow_conditional_preopen_order", False))
            and trading_status == "UNVERIFIED_PREOPEN"
            and asset_type in conditional_preopen_types
            and asset_type not in OPEN_END_FUND_TYPES
        )
        if trading_status != "TRADING":
            if conditional_preopen_status:
                reasons.append("PREOPEN_EXECUTION_STATUS_PENDING")
            else:
                reasons.append("NOT_TRADING")
        if asset.get("corporate_actions") and not asset.get(
            "history_adjusted_for_corporate_actions", False
        ):
            reasons.append("UNADJUSTED_CORPORATE_ACTION")

        hard_blocked = any(reason in hard_reason_names for reason in reasons)
        daily_return = float(asset.get("daily_return", 0.0))
        if hard_blocked:
            pass
        elif full_history and current_quantity and float(asset["close"]) < metrics["ma20"] * float(
            strategy["risk"]["exit_below_ma20_ratio"]
        ):
            action = "SELL"
            signal_type = "TREND_EXIT"
            score = 2.0 + abs(metrics["return20"])
            reasons.append("TREND_EXIT")
        elif asset_type in {"cash_etf", "money_market_fund"} and full_history:
            if metrics["volatility20"] <= float(strategy["risk"]["cash_etf_max_volatility"]):
                target_weight = float(strategy["risk"]["cash_etf_target_weight"])
                action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                    "BUY" if not current_quantity else "HOLD"
                )
                signal_type = "CASH_MANAGEMENT"
                score = 0.4
                reasons.append("CASH_MANAGEMENT_ELIGIBLE")
            else:
                reasons.append("CASH_ETF_VOLATILITY_TOO_HIGH")
        elif asset_type in {"bond_etf", "open_end_bond_fund"} and full_history:
            if float(asset["close"]) >= metrics["ma20"] and metrics["return20"] >= -0.005:
                target_weight = float(strategy["risk"]["bond_fund_target_weight"])
                action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                    "BUY" if not current_quantity else "HOLD"
                )
                signal_type = "FIXED_INCOME_TREND"
                score = 0.6 + metrics["return20"]
                reasons.append("FIXED_INCOME_TREND_ELIGIBLE")
            else:
                reasons.append("FIXED_INCOME_TREND_NOT_CONFIRMED")
        elif asset_type in {"gold_etf", "open_end_gold_fund"} and (
            full_history
            and float(asset["close"]) > metrics["ma20"]
            and metrics["ma5"] >= metrics["ma20"]
        ):
            target_weight = float(strategy["risk"]["gold_fund_target_weight"])
            action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                "BUY" if not current_quantity else "HOLD"
            )
            signal_type = "GOLD_TREND"
            score = 0.8 + metrics["return20"]
            reasons.append("GOLD_DIVERSIFIER_TREND_ELIGIBLE")
        elif asset_type == "gold_etf" and cold_start and abs(daily_return) <= float(
            defensive_rules.get("maximum_abs_daily_return", 0.01)
        ):
            target_weight = float(defensive_rules.get("cold_start_gold_target_weight", 0.08))
            action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                "BUY" if not current_quantity else "HOLD"
            )
            signal_type = "COLD_START_DEFENSIVE"
            score = 0.7
            reasons.append("COLD_START_GOLD_DIVERSIFIER")
        elif _is_equity_exposure(asset):
            dip_min = float(dip_rules.get("minimum_daily_return", -0.035))
            dip_max = float(dip_rules.get("maximum_daily_return", -0.01))
            dip_history_ok = full_history and float(asset["close"]) >= metrics["ma20"] * float(
                dip_rules.get("minimum_price_to_ma20", 0.94)
            )
            cold_dip_groups = set(cold_start_rules.get("dip_exposure_groups", []))
            dip_cold_ok = cold_start and str(asset.get("exposure_group", symbol)) in cold_dip_groups
            if (
                dip_min <= daily_return <= dip_max
                and (dip_history_ok or dip_cold_ok)
                and (not shock or bool(dip_rules.get("allow_during_broad_market_shock", False)))
            ):
                target_weight = _signal_target_weight(strategy, "controlled_dip", bucket, 0.05)
                action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                    "BUY" if not current_quantity else "HOLD"
                )
                signal_type = "CONTROLLED_DIP"
                score = 1.15 + min(abs(daily_return), 0.05) + (0.08 if dip_cold_ok else 0.0)
                reasons.append("CONTROLLED_DIP_ENTRY")
            elif shock:
                reasons.append("EQUITY_BUY_BLOCKED_BY_MARKET_SHOCK")
            else:
                trend_min = float(trend_rules.get("minimum_daily_return", 0.002))
                trend_max = float(trend_rules.get("maximum_daily_return", 0.025))
                full_trend = (
                    full_history
                    and float(asset["close"]) > metrics["ma20"]
                    and metrics["ma5"] >= metrics["ma20"]
                    and daily_return > float(strategy["risk"]["single_asset_shock_daily_return"])
                )
                cold_trend = cold_start and trend_min <= daily_return <= trend_max
                if full_trend or cold_trend:
                    target_weight = _signal_target_weight(
                        strategy,
                        "trend",
                        bucket,
                        _maximum_asset_weight(asset_type, strategy),
                    )
                    action = "ADD" if current_quantity and current_weight + 1e-9 < target_weight else (
                        "BUY" if not current_quantity else "HOLD"
                    )
                    signal_type = "TREND" if full_trend else "COLD_START_TREND"
                    bucket_bonus = 0.25 if bucket == "core_equity" else 0.0
                    score = 1.0 + bucket_bonus + min(max(daily_return, 0.0), trend_max)
                    reasons.append(
                        "POSITIVE_TREND_WITHOUT_SHOCK" if full_trend else "COLD_START_TREND_ENTRY"
                    )
                else:
                    reasons.append("TREND_NOT_CONFIRMED")
        else:
            reasons.append("TREND_NOT_CONFIRMED")

        recommendations.append(
            {
                "symbol": symbol,
                "name": asset["name"],
                "asset_type": asset["asset_type"],
                "risk_bucket": bucket,
                "exposure_group": asset.get("exposure_group", symbol),
                "action": action,
                "signal_type": signal_type,
                "score": round(score, 8),
                "current_weight": current_weight,
                "target_weight": target_weight,
                "reasons": sorted(set(reasons)),
                "metrics": metrics,
                "source_count": len(price_sources),
            }
        )

    has_pending_open = any(
        order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
    )
    has_action = any(
        item["action"] in ACTIONABLE_BUY_ACTIONS | ACTIONABLE_SELL_ACTIONS
        for item in recommendations
    )
    if (
        bool(daily_rules.get("enabled", False))
        and snapshot["is_trading_day"]
        and not warnings
        and not has_pending_open
        and not has_action
    ):
        by_symbol = {item["symbol"]: item for item in recommendations}
        eligible_types = set(daily_rules.get("eligible_asset_types", []))
        fallback_selected = False
        for symbol in daily_rules.get("fallback_preference", []):
            item = by_symbol.get(str(symbol))
            asset = assets.get(str(symbol))
            if not item or not asset or str(asset["asset_type"]) not in eligible_types:
                continue
            if shock and _is_equity_exposure(asset):
                continue
            if any(reason in hard_reason_names for reason in item["reasons"]):
                continue
            max_weight = _maximum_asset_weight(str(asset["asset_type"]), strategy)
            if item["current_weight"] >= max_weight - 1e-9:
                continue
            increment = float(daily_rules.get("fallback_increment_weight", 0.04))
            fallback_target = min(max_weight, item["current_weight"] + increment)
            lot = int(asset.get("lot_size", 100))
            current_quantity = int(
                ledger["positions"].get(str(symbol), {}).get("quantity", 0)
            )
            desired_quantity = (
                math.floor(portfolio_value * fallback_target / float(asset["close"]) / lot)
                * lot
            )
            if desired_quantity <= current_quantity:
                continue
            item["target_weight"] = fallback_target
            item["action"] = "ADD" if item["current_weight"] > 0 else "BUY"
            item["signal_type"] = "DAILY_EXPLORATION_FALLBACK"
            item["score"] = 0.1
            item["reasons"].append("DAILY_EXPLORATION_FALLBACK")
            fallback_selected = True
            break
        if not fallback_selected:
            trim_candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
            for item in recommendations:
                asset = assets.get(str(item["symbol"]))
                position = ledger["positions"].get(str(item["symbol"]))
                if not asset or not position or str(asset["asset_type"]) not in eligible_types:
                    continue
                if any(reason in hard_reason_names for reason in item["reasons"]):
                    continue
                lot = int(asset.get("lot_size", 100))
                if int(position.get("quantity", 0)) < lot:
                    continue
                if str(position.get("last_buy_date", "")) >= str(snapshot["run_date"]):
                    continue
                trim_candidates.append((float(item["current_weight"]), item, asset))
            if trim_candidates:
                _, item, asset = max(trim_candidates, key=lambda value: value[0])
                item["action"] = "REDUCE"
                item["order_quantity"] = int(asset.get("lot_size", 100))
                item["signal_type"] = "DAILY_REBALANCE_TRIM"
                item["score"] = 0.1
                item["reasons"].append("DAILY_REBALANCE_TRIM")

    return recommendations, sorted(set(global_blocks))


def _create_orders(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any], recommendations: list[dict[str, Any]], blocks: list[str]
) -> list[dict[str, Any]]:
    hard_blocks = [block for block in blocks if block != "BROAD_MARKET_SHOCK"]
    if (
        hard_blocks
        or not snapshot["is_trading_day"]
        or snapshot.get("market_state") not in {"closed", "preopen"}
    ):
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets, str(snapshot["as_of"]))
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
        mark = values["position_marks"][str(symbol)]
        price = float(mark["price"])
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

    exits = sorted(
        (item for item in recommendations if item["action"] in ACTIONABLE_SELL_ACTIONS),
        key=lambda item: (-float(item.get("score", 0.0)), str(item["symbol"])),
    )
    entries = sorted(
        (item for item in recommendations if item["action"] in ACTIONABLE_BUY_ACTIONS),
        key=lambda item: (-float(item.get("score", 0.0)), str(item["symbol"])),
    )
    for recommendation in exits + entries:
        symbol = recommendation["symbol"]
        if symbol in existing:
            continue
        asset = assets[symbol]
        if str(asset.get("asset_type")) in OPEN_END_FUND_TYPES:
            block_recommendation(recommendation, "USE_FUND_NAV_PIPELINE")
            continue
        bucket = str(asset.get("risk_bucket", _default_risk_bucket(asset)))
        group = str(asset.get("exposure_group", symbol))
        lot = int(asset.get("lot_size", 100))
        current_quantity = int(ledger["positions"].get(symbol, {}).get("quantity", 0))
        current_value = float(asset["close"]) * current_quantity
        if recommendation["action"] in ACTIONABLE_BUY_ACTIONS:
            if buy_count >= max_new_buys:
                block_recommendation(recommendation, "MAXIMUM_NEW_BUYS_REACHED")
                continue
            if current_quantity == 0 and len(used_symbols) >= max_positions:
                block_recommendation(recommendation, "MAXIMUM_POSITIONS_REACHED")
                continue
            if current_quantity == 0 and one_per_group and group in used_groups:
                block_recommendation(recommendation, "DUPLICATE_EXPOSURE_GROUP")
                continue
            desired_value = values["investable_value_cny"] * recommendation["target_weight"]
            if recommendation.get("signal_type") == "DAILY_EXPLORATION_FALLBACK":
                desired_value = max(
                    desired_value,
                    current_value
                    + float(
                        strategy.get("daily_execution", {}).get(
                            "minimum_notional_cny", 0.0
                        )
                    ),
                )
            desired_value = min(
                desired_value,
                portfolio_value * _maximum_asset_weight(str(asset["asset_type"]), strategy),
            )
            bucket_cap_value = portfolio_value * float(bucket_caps.get(bucket, 1.0))
            desired_value = min(
                desired_value,
                current_value + max(0.0, bucket_cap_value - bucket_values.get(bucket, 0.0)),
            )
            if _is_equity_exposure(asset):
                equity_cap_value = portfolio_value * float(strategy["risk"]["max_total_equity_weight"])
                desired_value = min(
                    desired_value,
                    current_value + max(0.0, equity_cap_value - equity_value),
                )
            purchase_capacity = max(
                0.0,
                float(ledger["cash_cny"]) - pending_buy_value - minimum_cash_value,
            )
            desired_value = min(desired_value, current_value + purchase_capacity)
            desired_quantity = math.floor(desired_value / float(asset["close"]) / lot) * lot
            quantity = max(0, desired_quantity - current_quantity)
            if quantity == 0:
                block_recommendation(recommendation, "POSITION_TOO_SMALL_AFTER_DIVERSIFICATION")
                continue
            planned_value = float(asset["close"]) * quantity
            recommendation["target_weight"] = (
                (current_value + planned_value) / portfolio_value if portfolio_value else 0.0
            )
            gap_limit = float(strategy["risk"]["maximum_next_open_gap"])
            if recommendation.get("signal_type") == "DAILY_EXPLORATION_FALLBACK":
                gap_limit = float(
                    strategy.get("daily_execution", {}).get(
                        "fallback_maximum_open_gap", gap_limit
                    )
                )
            limit = float(asset["close"]) * (1 + gap_limit)
            side = "BUY"
            buy_count += 1
            pending_buy_value += planned_value
            bucket_values[bucket] = bucket_values.get(bucket, 0.0) + planned_value
            if _is_equity_exposure(asset):
                equity_value += planned_value
            used_symbols.add(symbol)
            used_groups.add(group)
        else:
            position = ledger["positions"].get(symbol, {})
            if str(position.get("last_buy_date", "")) >= str(snapshot["run_date"]):
                block_recommendation(recommendation, "T_PLUS_ONE_SELL_BLOCK")
                continue
            requested = int(recommendation.get("order_quantity") or current_quantity)
            quantity = min(current_quantity, max(lot, requested // lot * lot))
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
            "signal_type": recommendation.get("signal_type", "UNKNOWN"),
            "signal_score": float(recommendation.get("score", 0.0)),
            "reasons": recommendation["reasons"],
        }
        ledger["pending_orders"].append(order)
        orders.append(order)
    return orders


def _fmt_pct(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value * 100:.2f}%"


def _render_report_legacy(
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


def _render_report(
    snapshot: dict[str, Any],
    ledger: dict[str, Any],
    values: dict[str, float],
    recommendations: list[dict[str, Any]],
    blocks: list[str],
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    input_hash: str,
    mode: str,
) -> str:
    status = "ORDERS_PENDING" if orders else ("FILLED" if fills else "NO_TRADE")
    mode_name = {"short": "收盘决策", "long": "长期复盘", "preopen": "盘前决策"}.get(mode, mode)
    total_pnl = values["project_equity_cny"] - float(ledger["initial_project_capital_cny"])
    lines = [
        f"# Vibe Finance {snapshot['run_date']} {mode_name}报告",
        "",
        f"- 状态：`{status}`",
        f"- 证据截点：{snapshot['as_of']}",
        f"- 输入 SHA-256：`{input_hash}`",
        f"- 项目总权益：¥{values['project_equity_cny']:.2f}",
        f"- 累计盈亏：{total_pnl:+.2f} 元",
        f"- 可投资现金：¥{values['cash_cny']:.2f}",
        f"- 持仓市值：¥{values['positions_cny']:.2f}",
        f"- DeepSeek 剩余预算：¥{values['infrastructure_remaining_cny']:.2f}",
        "",
        "## 数据与风险门禁",
        "",
    ]
    if blocks:
        lines.extend(f"- `{block}`" for block in blocks)
    else:
        lines.append("- 必要数据与组合约束已通过，可以登记虚拟订单。")
    lines.extend(
        [
            "",
            "## 决策",
            "",
            "| 代码 | 名称 | 动作 | 信号 | 评分 | 目标权重 | 依据 |",
            "|---|---|---:|---|---:|---:|---|",
        ]
    )
    for item in recommendations:
        lines.append(
            f"| {item['symbol']} | {item['name']} | {item['action']} | "
            f"{item.get('signal_type', 'NONE')} | {float(item.get('score', 0.0)):.4f} | "
            f"{_fmt_pct(item['target_weight'])} | {', '.join(item['reasons'])} |"
        )
    lines.extend(["", "## 虚拟成交与待执行订单", ""])
    if not fills and not orders:
        lines.append("- 本轮没有成交或新订单；具体原因见门禁与候选表。")
    for fill in fills:
        lines.append(
            f"- {fill['status']} {fill['side']} {fill['symbol']} {fill['quantity']} "
            f"@ {fill.get('fill_price', 'UNKNOWN')}"
        )
    for order in orders:
        lines.append(
            f"- PENDING {order['side']} {order['symbol']} {order['quantity']}；"
            f"信号 {order.get('signal_type', 'UNKNOWN')}；开盘价格上限 {order['limit_price']}。"
        )
    lines.extend(["", "## 证据", ""])
    for evidence in snapshot["evidence"]:
        lines.append(
            f"- [{evidence['title']}]({evidence['url']}) — {evidence['as_of']}，{evidence['tier']}级"
        )
    infra = ledger["research_infrastructure"]
    lines.extend(
        [
            "",
            "## DeepSeek 使用审计",
            "",
            f"- 实际调用：{infra['actual_calls']} 次",
            f"- 已记账成本：¥{infra['spent_cny']:.6f}",
            "- API 密钥不写入仓库、报告或自动化提示。",
            "",
            "## 边界",
            "",
            "本报告只服务于虚拟投资实验，不连接券商，不产生真实订单，也不构成任何投资建议。项目不分析或发布政治议题。",
            "",
        ]
    )
    return "\n".join(lines)


def _run_pipeline_locked(
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
    _ensure_ledger_schema(ledger)
    fills = _settle_pending(ledger, snapshot, strategy)
    recommendations, blocks = _recommendations(ledger, snapshot, strategy, warnings)
    orders = _create_orders(ledger, snapshot, strategy, recommendations, blocks)
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets, str(snapshot["as_of"]))
    _update_ledger_valuation(ledger, assets, values, str(snapshot["as_of"]))
    pending_open = sum(
        order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
    )
    daily_required = bool(strategy.get("daily_execution", {}).get("enabled", False))
    if not snapshot["is_trading_day"]:
        daily_execution_status = "NOT_APPLICABLE"
    elif pending_open:
        daily_execution_status = "ORDER_SCHEDULED"
    else:
        daily_execution_status = "FAILED_NO_OPEN_ORDER"
        if daily_required:
            blocks = sorted(set(blocks + ["DAILY_ORDER_REQUIREMENT_MISSED"]))
    input_hash = _sha256(input_path)
    strategy_hash = _sha256(strategy_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": mode,
        "input_sha256": input_hash,
        "strategy_sha256": strategy_hash,
        "as_of": snapshot["as_of"],
        "blocks": blocks,
        "recommendations": recommendations,
        "fills": fills,
        "new_orders": orders,
        "daily_execution_status": daily_execution_status,
        "valuation": values,
    }
    ledger["last_run_id"] = run_id
    ledger["run_history"].append(
        {"run_id": run_id, "run_date": snapshot["run_date"], "mode": mode, "input_sha256": input_hash}
    )
    report_text = _render_report(
        snapshot,
        ledger,
        values,
        recommendations,
        blocks,
        fills,
        orders,
        input_hash,
        mode,
    )
    event_payloads = _prepare_pipeline_events(
        events=fills + orders,
        run_id=run_id,
        mode=mode,
        input_path=input_path,
        strategy_path=strategy_path,
        decision_path=decision_path,
        decision=decision,
    )
    prepare_run_transaction(
        run_id=run_id,
        ledger_path=ledger_path,
        orders_log=orders_log,
        recorded_at=str(snapshot["as_of"]),
        portfolio=ledger,
        decision_path=decision_path,
        decision=decision,
        report_path=report_path,
        report_text=report_text,
        heartbeat={
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": mode,
            "input_sha256": input_hash,
        },
        events=event_payloads,
    )
    return {
        "status": (
            "FAILED_DAILY_ORDER"
            if daily_required and daily_execution_status == "FAILED_NO_OPEN_ORDER"
            else "PASS"
        ),
        "run_id": run_id,
        "report": str(report_path),
        "decision": str(decision_path),
        "blocks": blocks,
        "new_orders": len(orders),
        "fills": len(fills),
        "daily_execution_status": daily_execution_status,
        "project_equity_cny": values["project_equity_cny"],
    }


def _settle_pending_fund_orders(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any]
) -> list[dict[str, Any]]:
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    precision = int(strategy.get("fund_orders", {}).get("share_precision", 4))
    quantum = Decimal("1").scaleb(-precision)
    rounding_name = str(
        strategy.get("fund_orders", {}).get("share_rounding", "ROUND_HALF_UP")
    )
    if rounding_name != "ROUND_HALF_UP":
        raise DataGateError(f"不支持的基金份额舍入规则: {rounding_name}")
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
            shares = (net_subscription / nav).quantize(quantum, rounding=ROUND_HALF_UP)
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
                    "quantity": _decimal_string(new_quantity),
                    "average_cost": float((old_cost + amount) / new_quantity),
                    "name": asset["name"],
                    "asset_type": asset["asset_type"],
                    "risk_bucket": asset.get("risk_bucket", _default_risk_bucket(asset)),
                    "exposure_group": asset.get("exposure_group", order["symbol"]),
                    "acquired_date": position.get("acquired_date", nav_date),
                    "last_buy_date": nav_date,
                    "valuation_basis": "confirmed_nav",
                }
            )
            ledger["positions"][order["symbol"]] = position
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) - amount)
            order["confirmed_shares"] = _decimal_string(shares)
            order["gross_amount_cny"] = float(amount)
            order["purchase_fee_rate_applied"] = _decimal_string(fee_rate)
            trade_notional = amount
            realized_pnl = Decimal("0")
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
            cost_basis = _money(Decimal(str(position["average_cost"])) * shares)
            realized_pnl = gross - fee - cost_basis
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) + gross - fee)
            remaining = Decimal(str(position["quantity"])) - shares
            if remaining == 0:
                del ledger["positions"][order["symbol"]]
            else:
                position["quantity"] = _decimal_string(remaining)
            order["gross_amount_cny"] = float(gross)
            order["confirmed_shares"] = _decimal_string(shares)
            order["redemption_fee_rate_applied"] = _decimal_string(fee_rate)
            trade_notional = gross
        order["status"] = "FILLED"
        order["fill_date"] = nav_date
        order["fill_as_of"] = snapshot["as_of"]
        order["fill_nav"] = float(nav)
        order["fund_fee_cny"] = float(fee)
        order["total_fees_cny"] = float(fee)
        order["realized_pnl_cny"] = float(realized_pnl)
        order["settlement_kind"] = "CONFIRMED_NAV"
        order["share_precision"] = precision
        order["fund_share_rounding"] = rounding_name
        _record_filled_trade(
            ledger,
            side=str(order["side"]),
            notional=trade_notional,
            fees=fee,
            fill_date=nav_date,
            realized_pnl=realized_pnl,
        )
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
    values = _project_value(ledger, assets, str(snapshot["as_of"]))
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
        value = float(values["position_marks"][str(symbol)]["market_value_cny"])
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

    exits = sorted(
        (item for item in recommendations if item["action"] in ACTIONABLE_SELL_ACTIONS),
        key=lambda item: (-float(item.get("score", 0.0)), str(item["symbol"])),
    )
    entries = sorted(
        (item for item in recommendations if item["action"] in ACTIONABLE_BUY_ACTIONS),
        key=lambda item: (-float(item.get("score", 0.0)), str(item["symbol"])),
    )
    for recommendation in exits + entries:
        symbol = str(recommendation["symbol"])
        asset = assets[symbol]
        if str(asset.get("asset_type")) not in OPEN_END_FUND_TYPES:
            continue
        if symbol in existing_pending:
            continue
        bucket = str(asset.get("risk_bucket", _default_risk_bucket(asset)))
        group = str(asset.get("exposure_group", symbol))
        position = ledger["positions"].get(symbol)
        current_value = (
            float(asset["close"]) * float(position["quantity"]) if position else 0.0
        )
        if recommendation["action"] in ACTIONABLE_BUY_ACTIONS:
            if buy_count >= max_new_buys:
                block_item(recommendation, "MAXIMUM_NEW_BUYS_REACHED")
                continue
            if not position and len(used_symbols) >= max_positions:
                block_item(recommendation, "MAXIMUM_POSITIONS_REACHED")
                continue
            if (
                not position
                and diversification.get("one_position_per_exposure_group", False)
                and group in used_groups
            ):
                block_item(recommendation, "DUPLICATE_EXPOSURE_GROUP")
                continue
            desired_total = portfolio_value * float(recommendation["target_weight"])
            desired_total = min(
                desired_total,
                portfolio_value * _maximum_asset_weight(str(asset["asset_type"]), strategy),
            )
            desired_total = min(
                desired_total,
                current_value
                + max(
                    0.0,
                    portfolio_value * float(bucket_caps.get(bucket, 1.0))
                    - bucket_values.get(bucket, 0.0),
                ),
            )
            if _is_equity_exposure(asset):
                desired_total = min(
                    desired_total,
                    current_value
                    + max(
                        0.0,
                        portfolio_value
                        * float(strategy["risk"]["max_total_equity_weight"])
                        - equity_value,
                    ),
                )
            amount = _money(
                min(
                    max(0.0, desired_total - current_value),
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
            recommendation["target_weight"] = (
                (current_value + float(amount)) / portfolio_value if portfolio_value else 0.0
            )
            order_value: dict[str, Any] = {"amount_cny": float(amount)}
        else:
            if not position:
                continue
            side = "SELL"
            order_value = {
                "quantity": _decimal_string(
                    Decimal(
                        str(recommendation.get("order_quantity") or position["quantity"])
                    )
                )
            }
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
            "share_precision": int(
                strategy.get("fund_orders", {}).get("share_precision", 4)
            ),
            "fund_share_rounding": str(
                strategy.get("fund_orders", {}).get(
                    "share_rounding", "ROUND_HALF_UP"
                )
            ),
            "simulation_only": True,
            "signal_type": recommendation.get("signal_type", "UNKNOWN"),
            "signal_score": float(recommendation.get("score", 0.0)),
            "reasons": recommendation["reasons"],
            **order_value,
        }
        ledger["pending_orders"].append(order)
        orders.append(order)
    return orders


def _render_fund_report_legacy(
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
        f"# Vibe Finance {snapshot['run_date']} 场外基金净值报告",
        "",
        f"- 证据截点：{snapshot['as_of']}",
        f"- 输入 SHA-256：`{input_hash}`",
        f"- 项目总权益：¥{values['project_equity_cny']:.2f}",
        f"- 已确认净值成交：{sum(event.get('status') == 'FILLED' for event in events)}",
        f"- 新增待确认净值订单：{len(orders)}",
        "",
        "## 门禁",
        "",
    ]
    lines.extend((f"- `{block}`" for block in blocks),)
    if not blocks:
        lines.append("- 净值、申赎状态、费率和来源校验通过。")
    lines.extend(
        [
            "",
            "## 基金决策",
            "",
            "| 代码 | 名称 | 动作 | 信号 | 目标权重 | 原因 |",
            "|---|---|---:|---|---:|---|",
        ]
    )
    for item in recommendations:
        if str(item["asset_type"]) not in OPEN_END_FUND_TYPES:
            continue
        lines.append(
            f"| {item['symbol']} | {item['name']} | {item['action']} | "
            f"{item.get('signal_type', 'NONE')} | {_fmt_pct(item['target_weight'])} | "
            f"{', '.join(item['reasons'])} |"
        )
    lines.extend(["", "## 订单与成交", ""])
    if not events and not orders:
        lines.append("- 本轮没有场外基金订单或确认净值成交。")
    for event in events:
        lines.append(
            f"- {event['status']} {event['side']} {event['symbol']}；"
            f"确认净值 {event.get('fill_nav', 'UNKNOWN')}；费用 ¥{float(event.get('total_fees_cny', 0)):.2f}。"
        )
    for order in orders:
        size = order.get("amount_cny", order.get("quantity", "UNKNOWN"))
        lines.append(
            f"- PENDING_NEXT_NAV {order['side']} {order['symbol']}；申请规模 {size}。"
        )
    lines.extend(["", "## 证据", ""])
    for evidence in snapshot["evidence"]:
        lines.append(
            f"- [{evidence['title']}]({evidence['url']}) — {evidence['as_of']}，{evidence['tier']}级"
        )
    lines.extend(
        [
            "",
            "场外基金遵循未知价原则：订单登记后，只能使用信号日之后公开并完成交叉验证的确认净值结算。",
            "",
            "本报告仅用于虚拟实验，不构成任何投资建议；项目不分析或发布政治议题。",
            "",
        ]
    )
    return "\n".join(lines)


def _run_fund_nav_pipeline_locked(
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
    _ensure_ledger_schema(ledger)
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
    values = _project_value(ledger, assets, str(snapshot["as_of"]))
    _update_ledger_valuation(ledger, assets, values, str(snapshot["as_of"]))
    input_hash = _sha256(input_path)
    strategy_hash = _sha256(strategy_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": "fund_nav",
        "input_sha256": input_hash,
        "strategy_sha256": strategy_hash,
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
    report_text = _render_fund_report(
        snapshot, values, recommendations, events, orders, blocks, input_hash
    )
    event_payloads = _prepare_pipeline_events(
        events=events + orders,
        run_id=run_id,
        mode="fund_nav",
        input_path=input_path,
        strategy_path=strategy_path,
        decision_path=decision_path,
        decision=decision,
    )
    prepare_run_transaction(
        run_id=run_id,
        ledger_path=ledger_path,
        orders_log=orders_log,
        recorded_at=str(snapshot["as_of"]),
        portfolio=ledger,
        decision_path=decision_path,
        decision=decision,
        report_path=report_path,
        report_text=report_text,
        heartbeat={
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": "fund_nav",
            "input_sha256": input_hash,
        },
        events=event_payloads,
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


def _render_execution_report_legacy(
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
        f"- 估值后项目总权益：¥{values['project_equity_cny']:.2f}",
        "",
        "## 结算门禁",
        "",
    ]
    if blocks:
        lines.extend(f"- `{block}`" for block in blocks)
    else:
        lines.append("- 交易日、时间顺序、交易状态和双源价格均已通过。")
    lines.extend(["", "## 虚拟成交事件", ""])
    if not events:
        lines.append("- 没有可结算订单。")
    for event in events:
        if event.get("status") == "FILLED":
            lines.append(
                f"- FILLED {event['side']} {event['symbol']} {event['quantity']} @ "
                f"{event['fill_price']}；佣金 ¥{event['commission_cny']:.2f}，"
                f"过户费 ¥{event['transfer_fee_cny']:.2f}，印花税 ¥{event['stamp_tax_cny']:.2f}，"
                f"合计 ¥{event['total_fees_cny']:.2f}。"
            )
        else:
            lines.append(
                f"- {event['status']} {event['side']} {event['symbol']} {event['quantity']}；"
                f"{event.get('cancellation_reason', '未通过结算门禁')}。"
            )
    lines.extend(
        [
            "",
            "## 费用模型",
            "",
            f"- 模拟佣金：成交额的 {float(costs['commission_rate']) * 100:.3f}%，"
            f"单笔最低 ¥{float(costs['minimum_commission_cny']):.2f}。",
            f"- A股卖出印花税：成交额的 {float(costs['stock_sell_stamp_rate']) * 100:.3f}%。",
            "- 股票ETF不计证券交易印花税；所有费用仍按虚拟账本记录。",
            "",
            "本报告仅用于虚拟实验，不连接券商，不构成任何投资建议；项目不分析或发布政治议题。",
            "",
        ]
    )
    return "\n".join(lines)


def _settle_open_orders_locked(
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
    _ensure_ledger_schema(ledger)
    pending_before = sum(
        order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
    )
    blocks = list(warnings)
    if not snapshot["is_trading_day"]:
        blocks.append("NON_TRADING_DAY")
    if snapshot.get("market_state") not in {"open", "opening_auction_complete"}:
        blocks.append("MARKET_NOT_OPEN")
    daily_rules = strategy.get("daily_execution", {})
    daily_required = bool(daily_rules.get("enabled", False))
    if snapshot["is_trading_day"] and daily_required and pending_before == 0:
        blocks.append("NO_PENDING_DAILY_ORDER")
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
            valued["price_as_of"] = valued.get("open_price_as_of", snapshot["as_of"])
            valued["price_source_ids"] = list(valued.get("open_source_ids", []))
        valuation_assets[str(valued["symbol"])] = valued
    values = _project_value(ledger, valuation_assets, str(snapshot["as_of"]))
    blocks.extend(values["blocks"])
    _update_ledger_valuation(ledger, valuation_assets, values, str(snapshot["as_of"]))
    filled_count = sum(event.get("status") == "FILLED" for event in events)
    required_count = int(daily_rules.get("minimum_filled_trades_per_trading_day", 1))
    if not snapshot["is_trading_day"]:
        daily_execution_status = "NOT_APPLICABLE"
    elif filled_count >= required_count:
        daily_execution_status = "PASS"
    else:
        daily_execution_status = "FAILED_NO_FILL"
        if daily_required:
            blocks.append("DAILY_TRADE_REQUIREMENT_MISSED")
    blocks = sorted(set(blocks))
    input_hash = _sha256(input_path)
    strategy_hash = _sha256(strategy_path)
    run_id = uuid.uuid4().hex
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": snapshot["run_date"],
        "mode": "open_settlement",
        "input_sha256": input_hash,
        "strategy_sha256": strategy_hash,
        "as_of": snapshot["as_of"],
        "blocks": sorted(set(blocks)),
        "events": events,
        "pending_before": pending_before,
        "pending_after": sum(
            order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
        ),
        "daily_execution_status": daily_execution_status,
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
    report_text = _render_execution_report(
        snapshot,
        values,
        events,
        blocks,
        input_hash,
        pending_before,
        sum(
            order.get("status") == "PENDING_NEXT_OPEN"
            for order in ledger["pending_orders"]
        ),
        strategy,
    )
    event_payloads = _prepare_pipeline_events(
        events=events,
        run_id=run_id,
        mode="open_settlement",
        input_path=input_path,
        strategy_path=strategy_path,
        decision_path=decision_path,
        decision=decision,
    )
    prepare_run_transaction(
        run_id=run_id,
        ledger_path=ledger_path,
        orders_log=orders_log,
        recorded_at=str(snapshot["as_of"]),
        portfolio=ledger,
        decision_path=decision_path,
        decision=decision,
        report_path=report_path,
        report_text=report_text,
        heartbeat={
            "status": "ACTIVE",
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_date": snapshot["run_date"],
            "mode": "open_settlement",
            "input_sha256": input_hash,
        },
        events=event_payloads,
    )
    return {
        "status": (
            "FAILED_DAILY_TRADE"
            if daily_required and daily_execution_status == "FAILED_NO_FILL"
            else "PASS"
        ),
        "run_id": run_id,
        "report": str(report_path),
        "decision": str(decision_path),
        "blocks": blocks,
        "filled": filled_count,
        "cancelled": sum(
            str(event.get("status", "")).startswith("CANCELLED")
            for event in events
        ),
        "pending_after": sum(
            order.get("status") == "PENDING_NEXT_OPEN" for order in ledger["pending_orders"]
        ),
        "daily_execution_status": daily_execution_status,
        "project_equity_cny": values["project_equity_cny"],
    }


def update_readme_status(
    readme_path: Path = DEFAULT_README,
    ledger_path: Path = DEFAULT_LEDGER,
) -> dict[str, Any]:
    """Replace only the public ledger block in README with ledger-derived values."""
    start_marker = "<!-- VIBE_STATUS:START -->"
    end_marker = "<!-- VIBE_STATUS:END -->"
    source = readme_path.read_text(encoding="utf-8")
    if start_marker not in source or end_marker not in source:
        raise DataGateError("README 缺少 VIBE_STATUS 标记，拒绝改写其他内容")
    ledger = _read_json(ledger_path)
    _ensure_ledger_schema(ledger)
    infra = ledger["research_infrastructure"]
    performance = ledger["performance"]
    initial = float(ledger["initial_project_capital_cny"])
    cash = float(ledger["cash_cny"])
    infrastructure_remaining = float(
        _money(infra["reserved_cny"]) - _money(infra["spent_cny"])
    )
    positions_value = 0.0
    position_lines: list[str] = []
    for symbol, position in sorted(ledger["positions"].items()):
        quantity = float(position["quantity"])
        mark = float(position.get("last_price", position["average_cost"]))
        market_value = mark * quantity
        positions_value += market_value
        unrealized = market_value - float(position["average_cost"]) * quantity
        position_lines.append(
            f"| {symbol} | {position.get('name', symbol)} | {quantity:g} | "
            f"¥{float(position['average_cost']):,.4f} | ¥{mark:,.4f} | "
            f"¥{market_value:,.2f} | {unrealized:+,.2f} |"
        )
    investable = cash + positions_value
    project_equity = investable + infrastructure_remaining
    total_pnl = project_equity - initial
    total_return = total_pnl / initial if initial else 0.0
    valuation_as_of = performance.get("last_valuation_as_of") or "尚无成交后估值"
    pnl_state = "盈利" if total_pnl > 0 else ("亏损" if total_pnl < 0 else "持平")
    pending = [
        order
        for order in ledger["pending_orders"]
        if str(order.get("status", "")).startswith("PENDING")
    ]
    block = [
        start_marker,
        "### 公开实验账本",
        "",
        f"> 数据截点：`{valuation_as_of}`。状态由 `data/ledger/portfolio.json` 生成；所有金额均为虚拟记录。",
        "",
        "| 指标 | 当前值 |",
        "|---|---:|",
        f"| 初始项目资本 | ¥{initial:,.2f} |",
        f"| 累计买入金额 | ¥{float(performance['cumulative_buy_notional_cny']):,.2f} |",
        f"| 当前持仓市值 | ¥{positions_value:,.2f} |",
        f"| 可投资现金 | ¥{cash:,.2f} |",
        f"| 累计交易费用 | ¥{float(performance['cumulative_fees_cny']):,.2f} |",
        f"| DeepSeek 已用预算 | ¥{float(infra['spent_cny']):,.6f} |",
        f"| 项目总权益 | ¥{project_equity:,.2f} |",
        f"| 累计盈亏 | **{pnl_state} {total_pnl:+,.2f} 元（{total_return:+.2%}）** |",
        f"| 已成交笔数 | {int(performance['filled_trade_count'])} |",
        f"| 待执行订单 | {len(pending)} |",
        "",
        "#### 当前持仓",
        "",
    ]
    if position_lines:
        block.extend(
            [
                "| 代码 | 标的 | 数量 | 平均成本 | 最近估值 | 市值 | 未实现盈亏（元） |",
                "|---|---|---:|---:|---:|---:|---:|",
                *position_lines,
            ]
        )
    else:
        block.append("暂无已成交持仓。待成交订单会先进入账本，成交后才计入这里。")
    block.extend(["", end_marker])
    before, remainder = source.split(start_marker, 1)
    _, after = remainder.split(end_marker, 1)
    updated = before.rstrip() + "\n\n" + "\n".join(block) + after
    _atomic_text(readme_path, updated)
    return {
        "status": "UPDATED",
        "readme": str(readme_path),
        "project_equity_cny": round(project_equity, 2),
        "total_pnl_cny": round(total_pnl, 2),
        "positions": len(position_lines),
        "pending_orders": len(pending),
    }


def _project_status_locked(
    ledger_path: Path = DEFAULT_LEDGER,
    report_dir: Path = DEFAULT_REPORT_DIR,
    max_age_hours: float = 36.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only liveness and budget status; never refreshes the heartbeat."""
    heartbeat_path = ledger_path.parent / "heartbeat.json"
    if not ledger_path.exists():
        return {"status": "INACTIVE", "reason": "LEDGER_OR_HEARTBEAT_MISSING"}
    transaction_state = inspect_transaction_state(ledger_path)
    if transaction_state["status"] == "INCOMPLETE":
        return transaction_state
    if not heartbeat_path.exists():
        return {"status": "INACTIVE", "reason": "LEDGER_OR_HEARTBEAT_MISSING"}
    ledger = _read_json(ledger_path)
    _ensure_ledger_schema(ledger)
    heartbeat = _read_json(heartbeat_path)
    if heartbeat.get("run_id") != ledger.get("last_run_id"):
        return {
            "status": "INCOMPLETE",
            "reason": "HEARTBEAT_PORTFOLIO_RUN_ID_MISMATCH",
            "recoverable": False,
        }
    last_success = _parse_time(str(heartbeat["last_success_at"]))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_hours = (current.astimezone(last_success.tzinfo) - last_success).total_seconds() / 3600
    reports = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    infra = ledger["research_infrastructure"]
    remaining = _money(infra["reserved_cny"]) - _money(infra["spent_cny"])
    performance = ledger["performance"]
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
        "filled_trade_count": int(performance["filled_trade_count"]),
        "last_filled_trade_date": performance["last_filled_trade_date"],
        "project_equity_cny": float(performance["project_equity_cny"]),
        "total_pnl_cny": float(performance["total_pnl_cny"]),
        "total_return_pct": float(performance["total_return_pct"]),
        "deepseek_actual_calls": int(infra["actual_calls"]),
        "deepseek_spent_cny": float(_money(infra["spent_cny"])),
        "deepseek_remaining_cny": float(remaining),
    }


def _record_api_cost_locked(
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
    _ensure_ledger_schema(ledger)
    infra = ledger["research_infrastructure"]
    new_spent = _money(infra["spent_cny"]) + _money(amount_cny)
    if new_spent > _money(infra["reserved_cny"]):
        raise ValueError("DeepSeek 调用将超过 100 元研究预算")
    infra["spent_cny"] = float(new_spent)
    infra["actual_calls"] = int(infra["actual_calls"]) + 1
    positions_value = 0.0
    for symbol, position in ledger["positions"].items():
        if position.get("last_price") is None or not position.get("last_price_as_of"):
            raise DataGateError(f"MISSING_POSITION_MARK:{symbol}")
        positions_value += float(position["last_price"]) * float(position["quantity"])
    project_equity = (
        float(ledger["cash_cny"])
        + positions_value
        + float(_money(infra["reserved_cny"]) - new_spent)
    )
    initial = float(ledger["initial_project_capital_cny"])
    performance = ledger["performance"]
    performance["investable_value_cny"] = float(ledger["cash_cny"]) + positions_value
    performance["project_equity_cny"] = project_equity
    performance["total_pnl_cny"] = float(_money(project_equity - initial))
    performance["total_return_pct"] = (project_equity - initial) / initial if initial else 0.0
    timestamp = datetime.now(timezone.utc).isoformat()
    event = {
        "timestamp": timestamp,
        "provider": "DeepSeek",
        "model": model,
        "purpose": purpose,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "amount_cny": float(_money(amount_cny)),
        "actual_api_call": True,
    }
    run_id = uuid.uuid4().hex
    ledger["last_run_id"] = run_id
    ledger["run_history"].append(
        {
            "run_id": run_id,
            "run_date": timestamp[:10],
            "mode": "api_cost",
            "input_sha256": hashlib.sha256(
                json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
    )
    cost_line = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    existing_costs = cost_log.read_bytes() if cost_log.exists() else b""
    if existing_costs and not existing_costs.endswith(b"\n"):
        raise DataGateError("API cost log must end with a newline")
    input_hash = hashlib.sha256(cost_line).hexdigest()
    decision = {
        "schema_version": 1,
        "run_id": run_id,
        "run_date": timestamp[:10],
        "mode": "api_cost",
        "input_sha256": input_hash,
        "api_cost_event": event,
        "project_equity_cny": project_equity,
    }
    artifact_dir = ledger_path.parent / "api_cost_transactions"
    report_text = (
        "# DeepSeek API 成本事务\n\n"
        f"- run_id: `{run_id}`\n"
        f"- timestamp: {timestamp}\n"
        f"- amount_cny: {float(_money(amount_cny)):.2f}\n"
        f"- model: {model}\n"
        f"- purpose: {purpose}\n"
        "- actual_api_call: true\n"
    )
    prepare_run_transaction(
        run_id=run_id,
        ledger_path=ledger_path,
        orders_log=DEFAULT_ORDERS_LOG if ledger_path == DEFAULT_LEDGER else ledger_path.parent / "orders.jsonl",
        recorded_at=timestamp,
        portfolio=ledger,
        decision_path=artifact_dir / f"{run_id}.json",
        decision=decision,
        report_path=artifact_dir / f"{run_id}.md",
        report_text=report_text,
        heartbeat={
            "status": "ACTIVE",
            "last_success_at": timestamp,
            "run_id": run_id,
            "run_date": timestamp[:10],
            "mode": "api_cost",
            "input_sha256": input_hash,
        },
        events=[],
        auxiliary_files=[(cost_log, existing_costs + cost_line)],
    )
    return {
        "status": "RECORDED",
        "run_id": run_id,
        "actual_calls": infra["actual_calls"],
        "spent_cny": infra["spent_cny"],
        "remaining_cny": float(_money(infra["reserved_cny"]) - new_spent),
    }


def _recovered_result(commits: list[dict[str, Any]]) -> dict[str, Any]:
    latest = commits[-1]
    return {
        "status": "RECOVERED",
        "run_id": latest.get("run_id"),
        "recovered_transactions": len(commits),
        "decision": latest.get("decision_path"),
        "report": latest.get("report_path"),
    }


def run_pipeline(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
    mode: str = "short",
) -> dict[str, Any]:
    with locked_state(ledger_path, exclusive=True):
        recovered = recover_incomplete_transactions(ledger_path)
        if recovered:
            return _recovered_result(recovered)
        return _run_pipeline_locked(
            input_path,
            ledger_path,
            strategy_path,
            report_dir,
            orders_log,
            mode,
        )


def run_fund_nav_pipeline(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_FUND_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
) -> dict[str, Any]:
    with locked_state(ledger_path, exclusive=True):
        recovered = recover_incomplete_transactions(ledger_path)
        if recovered:
            return _recovered_result(recovered)
        return _run_fund_nav_pipeline_locked(
            input_path, ledger_path, strategy_path, report_dir, orders_log
        )


def settle_open_orders(
    input_path: Path,
    ledger_path: Path = DEFAULT_LEDGER,
    strategy_path: Path = DEFAULT_STRATEGY,
    report_dir: Path = DEFAULT_EXECUTION_REPORT_DIR,
    orders_log: Path = DEFAULT_ORDERS_LOG,
) -> dict[str, Any]:
    with locked_state(ledger_path, exclusive=True):
        recovered = recover_incomplete_transactions(ledger_path)
        if recovered:
            return _recovered_result(recovered)
        return _settle_open_orders_locked(
            input_path, ledger_path, strategy_path, report_dir, orders_log
        )


def project_status(
    ledger_path: Path = DEFAULT_LEDGER,
    report_dir: Path = DEFAULT_REPORT_DIR,
    max_age_hours: float = 36.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    with locked_state(ledger_path, exclusive=False):
        return _project_status_locked(ledger_path, report_dir, max_age_hours, now)


def record_api_cost(
    ledger_path: Path,
    cost_log: Path,
    amount_cny: float,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    with locked_state(ledger_path, exclusive=True):
        recover_incomplete_transactions(ledger_path)
        return _record_api_cost_locked(
            ledger_path,
            cost_log,
            amount_cny,
            model,
            purpose,
            input_tokens,
            output_tokens,
        )
