from __future__ import annotations

import hashlib
import time as time_module
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .open_capture import (
    DEFAULT_LEDGER,
    DEFAULT_STRATEGY,
    DEFAULT_UNIVERSE,
    SHANGHAI,
    SAFE_CORPORATE_ACTION_STATUSES,
    SAFE_ST_DELISTING_STATUSES,
    Clock,
    Fetch,
    OpenCaptureError,
    Sleeper,
    _default_fetch,
    _fetch_with_retry,
    _parse_clock,
    _parse_sina,
    _parse_tencent,
    _prices_match,
    _quote_url,
    _read_object,
    _required_assets,
    _write_json_exclusive,
)


def _capture_intraday_snapshot_once(
    *,
    base_snapshot_path: Path,
    output_path: Path,
    strategy_path: Path = DEFAULT_STRATEGY,
    universe_path: Path = DEFAULT_UNIVERSE,
    ledger_path: Path = DEFAULT_LEDGER,
    now: datetime | None = None,
    fetch: Fetch = _default_fetch,
    clock: Clock | None = None,
    sleeper: Sleeper = time_module.sleep,
) -> dict[str, Any]:
    """Seal a dual-source continuous-auction snapshot for failed-open recovery."""
    if output_path.exists():
        raise OpenCaptureError(f"refusing to overwrite immutable snapshot: {output_path}")
    base = _read_object(base_snapshot_path)
    strategy = _read_object(strategy_path)
    universe = _read_object(universe_path)
    ledger = _read_object(ledger_path)
    rules = strategy.get("data_collection", {}).get("intraday_recovery", {})
    if not bool(rules.get("enabled", False)):
        raise OpenCaptureError("intraday recovery is disabled")
    source_ids = list(rules.get("source_ids", []))
    if source_ids != ["tencent_finance", "sina_finance"]:
        raise OpenCaptureError("intraday_recovery.source_ids must pin Tencent then Sina")

    capture_clock: Clock
    if clock is not None:
        capture_clock = clock
    elif now is not None:
        capture_clock = lambda: now
    else:
        capture_clock = lambda: datetime.now(SHANGHAI)
    run_date = str(base.get("run_date", ""))
    window_start = _parse_clock(str(rules.get("window_start", "13:00:00")), "window_start")
    window_end = _parse_clock(str(rules.get("window_end", "14:50:00")), "window_end")

    all_symbols, metadata = _required_assets(base, universe, ledger, strategy)
    pending_symbols = {
        str(order["symbol"])
        for order in ledger.get("pending_orders", [])
        if order.get("status") == "PENDING_NEXT_OPEN"
    }
    held_symbols = {str(symbol) for symbol in ledger.get("positions", {})}
    critical_symbols = pending_symbols | held_symbols
    if not critical_symbols:
        raise OpenCaptureError("intraday recovery requires a pending order or position")
    symbols = sorted(critical_symbols)
    timeout = min(max(float(rules.get("request_timeout_seconds", 5.0)), 0.1), 10.0)
    attempts = min(max(int(rules.get("request_attempts", 3)), 1), 5)
    retry_delay = min(max(float(rules.get("retry_delay_seconds", 0.25)), 0.0), 2.0)
    raw_payloads: dict[str, bytes] = {}
    quote_sets: dict[str, Any] = {}
    urls: dict[str, str] = {}
    access_times: dict[str, datetime] = {}
    for source_id in source_ids:
        url = _quote_url(source_id, symbols)
        raw, accessed_at = _fetch_with_retry(
            source_id,
            url,
            timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            fetch=fetch,
            clock=capture_clock,
            sleeper=sleeper,
            run_date=run_date,
            window_start=window_start,
            window_end=window_end,
        )
        raw_payloads[source_id] = raw
        urls[source_id] = url
        access_times[source_id] = accessed_at
        quote_sets[source_id] = (
            _parse_tencent(raw) if source_id == "tencent_finance" else _parse_sina(raw)
        )

    tolerance = Decimal(str(rules.get("maximum_relative_price_difference", "0.0005")))
    max_skew = float(rules.get("maximum_source_skew_seconds", 30))
    max_age = float(rules.get("maximum_quote_age_seconds", 45))
    assets: list[dict[str, Any]] = []
    quote_times: list[datetime] = []
    for symbol in all_symbols:
        if symbol not in critical_symbols:
            assets.append(dict(metadata[symbol]))
            continue
        left = quote_sets["tencent_finance"].get(symbol)
        right = quote_sets["sina_finance"].get(symbol)
        if left is None or right is None:
            raise OpenCaptureError(f"two-source quote missing for {symbol}")
        if not left.name or not right.name:
            raise OpenCaptureError(f"source identity name missing for {symbol}")
        if left.observed_at.date().isoformat() != run_date or right.observed_at.date().isoformat() != run_date:
            raise OpenCaptureError(f"quote date mismatch: {symbol}")
        if left.observed_at > access_times[left.source_id] or right.observed_at > access_times[right.source_id]:
            raise OpenCaptureError(f"source timestamp is in the future: {symbol}")
        if abs((left.observed_at - right.observed_at).total_seconds()) > max_skew:
            raise OpenCaptureError(f"source timestamps are too far apart: {symbol}")
        if not _prices_match(left.current_price, right.current_price, tolerance):
            raise OpenCaptureError(f"intraday price conflict: {symbol}")
        if not _prices_match(left.previous_close, right.previous_close, tolerance):
            raise OpenCaptureError(f"previous-close conflict: {symbol}")
        latest_access = max(access_times.values())
        quote_age = max(
            (latest_access - left.observed_at).total_seconds(),
            (latest_access - right.observed_at).total_seconds(),
        )
        if symbol in critical_symbols and quote_age > max_age:
            raise OpenCaptureError(f"critical quote is stale: {symbol}:{quote_age:.1f}s")
        if symbol in critical_symbols and (left.volume <= 0 or right.volume <= 0):
            raise OpenCaptureError(f"critical quote has non-positive volume: {symbol}")
        quote_times.extend((left.observed_at, right.observed_at))

        base_asset = dict(metadata[symbol])
        primary_sources = list(base_asset.get("primary_source_ids", []))
        prior_sources = list(base_asset.get("source_ids", []))
        identity_status = str(base_asset.get("security_identity_status", "UNVERIFIED_PRIMARY_IDENTITY"))
        st_status = str(base_asset.get("st_delisting_status", "UNVERIFIED_TERMINATION_STATUS"))
        corporate_status = str(base_asset.get("corporate_action_status", "UNVERIFIED_PREOPEN"))
        execution_price = max(left.current_price, right.current_price)
        observed_at = max(left.observed_at, right.observed_at)
        trading_status = "TRADING"
        unresolved: list[str] = []
        if symbol in pending_symbols:
            if not identity_status.startswith("VERIFIED_ETF_"):
                unresolved.append("SECURITY_IDENTITY")
            if st_status not in SAFE_ST_DELISTING_STATUSES:
                unresolved.append("ST_DELISTING")
            if corporate_status not in SAFE_CORPORATE_ACTION_STATUSES:
                unresolved.append("CORPORATE_ACTION")
        if unresolved:
            trading_status = "UNVERIFIED_" + "_AND_".join(unresolved)
        asset = {
            "symbol": symbol,
            "name": str(base_asset.get("name") or left.name),
            "asset_type": str(base_asset["asset_type"]),
            "close": float(execution_price),
            "execution_price": float(execution_price),
            "daily_return": float(execution_price / left.previous_close - 1),
            "lot_size": int(base_asset.get("lot_size", 100)),
            "history": [],
            "source_ids": sorted(set(prior_sources + primary_sources + source_ids)),
            "price_source_ids": source_ids,
            "execution_source_ids": source_ids,
            "primary_source_ids": primary_sources,
            "risk_bucket": str(base_asset.get("risk_bucket", "unclassified_equity")),
            "exposure_group": str(base_asset.get("exposure_group", symbol)),
            "order_engine": str(base_asset.get("order_engine", "next_open")),
            "price_as_of": observed_at.isoformat(),
            "execution_price_as_of": observed_at.isoformat(),
            "trading_status": trading_status,
            "security_identity_status": identity_status,
            "suspension_status": "TRADING_NONZERO_VOLUME" if left.volume > 0 and right.volume > 0 else "UNVERIFIED_VOLUME",
            "st_delisting_status": st_status,
            "corporate_action_status": corporate_status,
            "corporate_actions": list(base_asset.get("corporate_actions", [])),
            "history_adjusted_for_corporate_actions": bool(base_asset.get("history_adjusted_for_corporate_actions", False)),
            "provider_names": {"tencent_finance": left.name, "sina_finance": right.name},
            "source_prices": {
                "tencent_finance": float(left.current_price),
                "sina_finance": float(right.current_price),
            },
            "quote_age_seconds": quote_age,
            "quality": "INTRADAY_DUAL_SOURCE_CONSERVATIVE_BUY_PRICE",
        }
        if "fund_metadata" in base_asset:
            asset["fund_metadata"] = base_asset["fund_metadata"]
        assets.append(asset)

    as_of = max(max(access_times.values()), max(quote_times))
    sources = [
        {
            "id": source_id,
            "url": urls[source_id],
            "accessed_at": access_times[source_id].isoformat(),
            "response_sha256": hashlib.sha256(raw_payloads[source_id]).hexdigest(),
            "tier": "C",
        }
        for source_id in source_ids
    ]
    evidence = list(base.get("evidence", [])) + [
        {
            "title": "盘中连续竞价双源行情封存",
            "url": urls[source_id],
            "published_at": as_of.isoformat(),
            "as_of": as_of.isoformat(),
            "tier": "C",
            "source_id": source_id,
            "response_sha256": hashlib.sha256(raw_payloads[source_id]).hexdigest(),
        }
        for source_id in source_ids
    ]
    snapshot = {
        "schema_version": 2,
        "run_date": run_date,
        "as_of": as_of.isoformat(),
        "is_trading_day": bool(base.get("is_trading_day")),
        "market_state": "continuous_trading",
        "simulation_only": True,
        "collection_note": "开盘抓取失败后的盘中恢复快照；只结算更早已存在的虚拟条件单，不生成盘中新信号。买入使用双源较高价。",
        "indices": list(base.get("indices", [])),
        "assets": assets,
        "sources": sources,
        "evidence": evidence,
        "deepseek_usage": {"actual_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0},
    }
    _write_json_exclusive(output_path, snapshot)
    return {
        "status": "SEALED",
        "output": str(output_path),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "as_of": as_of.isoformat(),
        "symbols": all_symbols,
        "captured_symbols": symbols,
        "sources": source_ids,
        "pending_symbols": sorted(pending_symbols),
    }


def capture_intraday_snapshot(
    *,
    base_snapshot_path: Path,
    output_path: Path,
    strategy_path: Path = DEFAULT_STRATEGY,
    universe_path: Path = DEFAULT_UNIVERSE,
    ledger_path: Path = DEFAULT_LEDGER,
    now: datetime | None = None,
    fetch: Fetch = _default_fetch,
    clock: Clock | None = None,
    sleeper: Sleeper = time_module.sleep,
) -> dict[str, Any]:
    """Retry transient cross-source refresh skew without relaxing any gate."""
    strategy = _read_object(strategy_path)
    rules = strategy.get("data_collection", {}).get("intraday_recovery", {})
    attempts = min(max(int(rules.get("consistency_attempts", 5)), 1), 8)
    delay = min(max(float(rules.get("consistency_retry_delay_seconds", 0.5)), 0.0), 3.0)
    transient_markers = (
        "intraday price conflict",
        "source timestamps are too far apart",
        "critical quote is stale",
    )
    last_error: OpenCaptureError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _capture_intraday_snapshot_once(
                base_snapshot_path=base_snapshot_path,
                output_path=output_path,
                strategy_path=strategy_path,
                universe_path=universe_path,
                ledger_path=ledger_path,
                now=now,
                fetch=fetch,
                clock=clock,
                sleeper=sleeper,
            )
        except OpenCaptureError as exc:
            last_error = exc
            if output_path.exists() or not any(marker in str(exc) for marker in transient_markers):
                raise
            if attempt == attempts:
                break
            sleeper(delay * attempt)
    raise OpenCaptureError(
        f"intraday quote consistency failed after {attempts} attempts: {last_error}"
    ) from last_error
