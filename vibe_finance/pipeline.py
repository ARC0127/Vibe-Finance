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
DEFAULT_STRATEGY = Path("config/strategy.json")
CENT = Decimal("0.01")


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
        positions_value += _money(float(price) * int(position["quantity"]))
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


def _settle_pending(
    ledger: dict[str, Any], snapshot: dict[str, Any], strategy: dict[str, Any]
) -> list[dict[str, Any]]:
    if not snapshot["is_trading_day"] or snapshot.get("market_state") != "closed":
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    events: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for order in ledger["pending_orders"]:
        asset = assets.get(order["symbol"])
        if not asset or snapshot["run_date"] <= order["signal_date"]:
            still_pending.append(order)
            continue
        if asset.get("open") is None or len(set(asset.get("source_ids", []))) < 2:
            order["status"] = "CANCELLED_DATA_GATE"
            events.append(order)
            continue
        price = float(asset["open"])
        quantity = int(order["quantity"])
        notional = _money(price * quantity)
        commission = max(
            _money(strategy["costs"]["minimum_commission_cny"]),
            _money(notional * Decimal(str(strategy["costs"]["commission_rate"]))),
        )
        if order["side"] == "BUY":
            total = notional + commission
            if price > float(order["limit_price"]) or total > _money(ledger["cash_cny"]):
                order["status"] = "CANCELLED_LIMIT_OR_CASH"
                events.append(order)
                continue
            position = ledger["positions"].get(order["symbol"], {"quantity": 0, "average_cost": 0.0})
            old_cost = _money(position["average_cost"] * position["quantity"])
            new_quantity = int(position["quantity"]) + quantity
            position["quantity"] = new_quantity
            position["average_cost"] = float((old_cost + total) / new_quantity)
            position["name"] = asset["name"]
            position["asset_type"] = asset["asset_type"]
            ledger["positions"][order["symbol"]] = position
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) - total)
        else:
            position = ledger["positions"].get(order["symbol"])
            if not position or quantity > int(position["quantity"]):
                order["status"] = "CANCELLED_POSITION"
                events.append(order)
                continue
            stamp = Decimal("0")
            if asset["asset_type"] == "stock":
                stamp = _money(notional * Decimal(str(strategy["costs"]["stock_sell_stamp_rate"])))
            ledger["cash_cny"] = float(_money(ledger["cash_cny"]) + notional - commission - stamp)
            position["quantity"] = int(position["quantity"]) - quantity
            if position["quantity"] == 0:
                del ledger["positions"][order["symbol"]]
        order["status"] = "FILLED"
        order["fill_date"] = snapshot["run_date"]
        order["fill_price"] = price
        order["commission_cny"] = float(commission)
        events.append(order)
    ledger["pending_orders"] = still_pending
    return events


def _market_shock(snapshot: dict[str, Any], strategy: dict[str, Any]) -> bool:
    threshold = float(strategy["risk"]["broad_market_shock_daily_return"])
    return any(
        item.get("broad", False) and float(item["daily_return"]) <= threshold
        for item in snapshot["indices"]
    )


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
        metrics = _metrics(asset)
        reasons: list[str] = []
        action = "WATCH"
        target_weight = 0.0
        held = symbol in ledger["positions"]
        if len(set(asset.get("source_ids", []))) < min_sources:
            reasons.append("PRICE_NOT_CROSSCHECKED")
        if metrics is None or len(asset.get("history", [])) < min_history:
            reasons.append("INSUFFICIENT_HISTORY")
        if any(reason in reasons for reason in ("PRICE_NOT_CROSSCHECKED", "INSUFFICIENT_HISTORY")):
            action = "HOLD" if held else "WATCH"
        elif held and float(asset["close"]) < metrics["ma20"] * float(strategy["risk"]["exit_below_ma20_ratio"]):
            action = "SELL"
            reasons.append("TREND_EXIT")
        elif asset["asset_type"] == "cash_etf":
            if metrics["volatility20"] <= float(strategy["risk"]["cash_etf_max_volatility"]):
                action = "HOLD" if held else "BUY"
                target_weight = float(strategy["risk"]["cash_etf_target_weight"])
                reasons.append("CASH_MANAGEMENT_ELIGIBLE")
            else:
                reasons.append("CASH_ETF_VOLATILITY_TOO_HIGH")
        elif shock:
            action = "HOLD" if held else "WATCH"
            reasons.append("EQUITY_BUY_BLOCKED_BY_MARKET_SHOCK")
        elif (
            float(asset["close"]) > metrics["ma20"]
            and metrics["ma5"] >= metrics["ma20"]
            and float(asset.get("daily_return", 0)) > float(strategy["risk"]["single_asset_shock_daily_return"])
        ):
            action = "HOLD" if held else "BUY"
            target_weight = float(strategy["risk"]["max_single_etf_weight"])
            if asset["asset_type"] == "stock":
                target_weight = float(strategy["risk"]["max_single_stock_weight"])
            reasons.append("POSITIVE_TREND_WITHOUT_SHOCK")
        else:
            action = "HOLD" if held else "WATCH"
            reasons.append("TREND_NOT_CONFIRMED")
        recommendations.append(
            {
                "symbol": symbol,
                "name": asset["name"],
                "asset_type": asset["asset_type"],
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
    if blocks or not snapshot["is_trading_day"] or snapshot.get("market_state") != "closed":
        return []
    assets = {str(item["symbol"]): item for item in snapshot["assets"]}
    values = _project_value(ledger, assets)
    existing = {order["symbol"] for order in ledger["pending_orders"]}
    orders: list[dict[str, Any]] = []
    for recommendation in recommendations:
        symbol = recommendation["symbol"]
        if symbol in existing or recommendation["action"] not in ("BUY", "SELL"):
            continue
        asset = assets[symbol]
        lot = int(asset.get("lot_size", 100))
        current_quantity = int(ledger["positions"].get(symbol, {}).get("quantity", 0))
        if recommendation["action"] == "BUY":
            desired_value = values["investable_value_cny"] * recommendation["target_weight"]
            desired_quantity = math.floor(desired_value / float(asset["close"]) / lot) * lot
            quantity = max(0, desired_quantity - current_quantity)
            if quantity == 0:
                continue
            limit = float(asset["close"]) * (1 + float(strategy["risk"]["maximum_next_open_gap"]))
            side = "BUY"
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
