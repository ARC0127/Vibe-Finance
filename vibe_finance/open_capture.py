from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


DEFAULT_STRATEGY = Path("config/strategy.json")
DEFAULT_UNIVERSE = Path("config/universe.json")
DEFAULT_LEDGER = Path("data/ledger/portfolio.json")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SAFE_CORPORATE_ACTION_STATUSES = {
    "CLEARED",
    "NO_UNADJUSTED_ACTION_FOUND_AT_CUTOFF",
}
SAFE_ST_DELISTING_STATUSES = {
    "CLEARED",
    "ETF_NOT_ST_AND_NO_TERMINATION_EVIDENCE_AT_CUTOFF",
}


class OpenCaptureError(RuntimeError):
    """Raised when an immutable open snapshot cannot be proved timely and consistent."""


@dataclass(frozen=True)
class Quote:
    source_id: str
    symbol: str
    name: str
    open_price: Decimal
    previous_close: Decimal
    current_price: Decimal
    volume: int
    observed_at: datetime


Fetch = Callable[[str, str, float], bytes]


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OpenCaptureError(f"JSON root must be an object: {path}")
    return value


def _market_symbol(symbol: str) -> str:
    if not re.fullmatch(r"\d{6}", symbol):
        raise OpenCaptureError(f"unsupported security code: {symbol!r}")
    return ("sh" if symbol[0] in {"5", "6"} else "sz") + symbol


def _quote_url(source_id: str, symbols: list[str]) -> str:
    codes = ",".join(_market_symbol(symbol) for symbol in symbols)
    if source_id == "tencent_finance":
        return f"https://qt.gtimg.cn/q={codes}"
    if source_id == "sina_finance":
        return f"https://hq.sinajs.cn/list={codes}"
    raise OpenCaptureError(f"unsupported open quote source: {source_id}")


def _default_fetch(source_id: str, url: str, timeout: float) -> bytes:
    headers = {"User-Agent": "Vibe-Finance/0.3 (+virtual-research)"}
    if source_id == "sina_finance":
        headers["Referer"] = "https://finance.sina.com.cn/"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise OpenCaptureError(f"{source_id} response exceeds 1 MB")
    if not payload:
        raise OpenCaptureError(f"{source_id} returned an empty response")
    return payload


def _positive_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise OpenCaptureError(f"invalid {field}: {raw!r}") from exc
    if value <= 0:
        raise OpenCaptureError(f"{field} must be positive")
    return value


def _parse_tencent(payload: bytes) -> dict[str, Quote]:
    text = payload.decode("gb18030")
    quotes: dict[str, Quote] = {}
    for _, _, body in re.findall(r'v_(sh|sz)(\d{6})="([^"]*)";', text):
        fields = body.split("~")
        if len(fields) <= 30:
            continue
        symbol = fields[2]
        try:
            observed_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
                tzinfo=SHANGHAI
            )
            volume = int(Decimal(fields[6]))
        except (ValueError, ArithmeticError) as exc:
            raise OpenCaptureError(f"invalid Tencent quote for {symbol}") from exc
        quotes[symbol] = Quote(
            source_id="tencent_finance",
            symbol=symbol,
            name=fields[1],
            open_price=_positive_decimal(fields[5], f"Tencent {symbol} open"),
            previous_close=_positive_decimal(fields[4], f"Tencent {symbol} previous close"),
            current_price=_positive_decimal(fields[3], f"Tencent {symbol} current"),
            volume=volume,
            observed_at=observed_at,
        )
    return quotes


def _parse_sina(payload: bytes) -> dict[str, Quote]:
    text = payload.decode("gb18030")
    quotes: dict[str, Quote] = {}
    for _, symbol, body in re.findall(r'var hq_str_(sh|sz)(\d{6})="([^"]*)";', text):
        fields = body.split(",")
        if len(fields) <= 31:
            continue
        try:
            observed_at = datetime.fromisoformat(f"{fields[30]}T{fields[31]}").replace(
                tzinfo=SHANGHAI
            )
            volume = int(Decimal(fields[8]))
        except (ValueError, ArithmeticError) as exc:
            raise OpenCaptureError(f"invalid Sina quote for {symbol}") from exc
        quotes[symbol] = Quote(
            source_id="sina_finance",
            symbol=symbol,
            name=fields[0],
            open_price=_positive_decimal(fields[1], f"Sina {symbol} open"),
            previous_close=_positive_decimal(fields[2], f"Sina {symbol} previous close"),
            current_price=_positive_decimal(fields[3], f"Sina {symbol} current"),
            volume=volume,
            observed_at=observed_at,
        )
    return quotes


def _parse_clock(raw: str, field: str) -> time:
    try:
        return time.fromisoformat(raw)
    except ValueError as exc:
        raise OpenCaptureError(f"invalid {field}: {raw!r}") from exc


def _prices_match(left: Decimal, right: Decimal, relative_tolerance: Decimal) -> bool:
    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= scale * relative_tolerance


def _required_assets(
    base: dict[str, Any],
    universe: dict[str, Any],
    ledger: dict[str, Any],
    strategy: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    allowed_types = set(
        strategy.get("data_collection", {}).get("daily_snapshot_asset_types", [])
    )
    universe_assets = {
        str(item["symbol"]): dict(item)
        for item in universe.get("listed_funds", [])
        if str(item.get("asset_type")) in allowed_types
    }
    base_assets = {str(item["symbol"]): dict(item) for item in base.get("assets", [])}
    required = set(universe_assets) | set(base_assets) | set(ledger.get("positions", {}))
    required.update(
        str(order["symbol"])
        for order in ledger.get("pending_orders", [])
        if order.get("status") == "PENDING_NEXT_OPEN"
    )
    metadata: dict[str, dict[str, Any]] = {}
    for symbol in required:
        merged = dict(universe_assets.get(symbol, {}))
        merged.update(base_assets.get(symbol, {}))
        if not merged:
            raise OpenCaptureError(f"required symbol is absent from universe and base snapshot: {symbol}")
        metadata[symbol] = merged
    return sorted(required), metadata


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise OpenCaptureError(f"refusing to overwrite immutable snapshot: {path}") from exc


def capture_open_snapshot(
    *,
    base_snapshot_path: Path,
    output_path: Path,
    strategy_path: Path = DEFAULT_STRATEGY,
    universe_path: Path = DEFAULT_UNIVERSE,
    ledger_path: Path = DEFAULT_LEDGER,
    now: datetime | None = None,
    fetch: Fetch = _default_fetch,
) -> dict[str, Any]:
    """Capture and immutably seal two-source opens inside the configured window."""
    if output_path.exists():
        raise OpenCaptureError(f"refusing to overwrite immutable snapshot: {output_path}")
    base = _read_object(base_snapshot_path)
    strategy = _read_object(strategy_path)
    universe = _read_object(universe_path)
    ledger = _read_object(ledger_path)
    rules = strategy.get("data_collection", {}).get("open_capture", {})
    source_ids = list(rules.get("source_ids", []))
    if source_ids != ["tencent_finance", "sina_finance"]:
        raise OpenCaptureError("open_capture.source_ids must pin Tencent then Sina")

    captured_at = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    run_date = str(base.get("run_date", ""))
    if captured_at.date().isoformat() != run_date:
        raise OpenCaptureError("capture date does not match base snapshot run_date")
    window_start = _parse_clock(str(rules.get("window_start", "09:30:00")), "window_start")
    window_end = _parse_clock(str(rules.get("window_end", "09:35:00")), "window_end")
    if not window_start <= captured_at.time().replace(tzinfo=None) <= window_end:
        raise OpenCaptureError("capture attempted outside the 09:30-09:35 Asia/Shanghai window")

    symbols, metadata = _required_assets(base, universe, ledger, strategy)
    timeout = min(max(float(rules.get("request_timeout_seconds", 5.0)), 0.1), 10.0)
    raw_payloads: dict[str, bytes] = {}
    quote_sets: dict[str, dict[str, Quote]] = {}
    urls: dict[str, str] = {}
    for source_id in source_ids:
        url = _quote_url(source_id, symbols)
        raw = fetch(source_id, url, timeout)
        raw_payloads[source_id] = raw
        urls[source_id] = url
        quote_sets[source_id] = (
            _parse_tencent(raw) if source_id == "tencent_finance" else _parse_sina(raw)
        )

    tolerance = Decimal(str(rules.get("maximum_relative_price_difference", "0")))
    max_skew = float(rules.get("maximum_source_skew_seconds", 30))
    assets: list[dict[str, Any]] = []
    quote_times: list[datetime] = []
    pending_symbols = {
        str(order["symbol"])
        for order in ledger.get("pending_orders", [])
        if order.get("status") == "PENDING_NEXT_OPEN"
    }
    for symbol in symbols:
        left = quote_sets["tencent_finance"].get(symbol)
        right = quote_sets["sina_finance"].get(symbol)
        if left is None or right is None:
            raise OpenCaptureError(f"two-source quote missing for {symbol}")
        if not left.name or not right.name:
            raise OpenCaptureError(f"source identity name missing for {symbol}")
        for quote in (left, right):
            quote_clock = quote.observed_at.time().replace(tzinfo=None)
            if quote.observed_at.date().isoformat() != run_date or not (
                window_start <= quote_clock <= window_end
            ):
                raise OpenCaptureError(f"source timestamp is outside the open window: {symbol}")
            if quote.observed_at > captured_at:
                raise OpenCaptureError(f"source timestamp is in the future: {symbol}")
            if quote.volume <= 0:
                raise OpenCaptureError(f"non-positive opening volume: {symbol}")
        if abs((left.observed_at - right.observed_at).total_seconds()) > max_skew:
            raise OpenCaptureError(f"source timestamps are too far apart: {symbol}")
        if not _prices_match(left.open_price, right.open_price, tolerance):
            raise OpenCaptureError(f"open price conflict: {symbol}")
        if not _prices_match(left.previous_close, right.previous_close, tolerance):
            raise OpenCaptureError(f"previous-close conflict: {symbol}")
        quote_times.extend((left.observed_at, right.observed_at))

        base_asset = dict(metadata[symbol])
        primary_sources = list(base_asset.get("primary_source_ids", []))
        prior_sources = list(base_asset.get("source_ids", []))
        identity_status = str(
            base_asset.get("security_identity_status", "UNVERIFIED_PRIMARY_IDENTITY")
        )
        st_delisting_status = str(
            base_asset.get("st_delisting_status", "UNVERIFIED_TERMINATION_STATUS")
        )
        corporate_status = str(base_asset.get("corporate_action_status", "UNVERIFIED_PREOPEN"))
        asset = {
            "symbol": symbol,
            "name": str(base_asset.get("name") or left.name),
            "asset_type": str(base_asset["asset_type"]),
            "close": float(left.previous_close),
            "open": float(left.open_price),
            "daily_return": float(left.open_price / left.previous_close - 1),
            "lot_size": int(base_asset.get("lot_size", 100)),
            "history": list(base_asset.get("history", [])),
            "source_ids": sorted(set(prior_sources + primary_sources + source_ids)),
            "price_source_ids": source_ids,
            "open_source_ids": source_ids,
            "primary_source_ids": primary_sources,
            "risk_bucket": str(base_asset.get("risk_bucket", "unclassified_equity")),
            "exposure_group": str(base_asset.get("exposure_group", symbol)),
            "order_engine": str(base_asset.get("order_engine", "next_open")),
            "price_as_of": max(left.observed_at, right.observed_at).isoformat(),
            "open_price_as_of": max(left.observed_at, right.observed_at).isoformat(),
            "trading_status": "TRADING",
            "security_identity_status": identity_status,
            "suspension_status": "TRADING_NONZERO_VOLUME",
            "st_delisting_status": st_delisting_status,
            "corporate_action_status": corporate_status,
            "corporate_actions": list(base_asset.get("corporate_actions", [])),
            "history_adjusted_for_corporate_actions": bool(
                base_asset.get("history_adjusted_for_corporate_actions", False)
            ),
            "open_volume_units": min(left.volume, right.volume),
            "provider_names": {
                "tencent_finance": left.name,
                "sina_finance": right.name,
            },
            "quality": "OPEN_DUAL_SOURCE_MATCH_NONZERO_VOLUME",
        }
        if symbol in pending_symbols:
            unresolved: list[str] = []
            if not identity_status.startswith("VERIFIED_ETF_"):
                unresolved.append("SECURITY_IDENTITY")
            if st_delisting_status not in SAFE_ST_DELISTING_STATUSES:
                unresolved.append("ST_DELISTING")
            if corporate_status not in SAFE_CORPORATE_ACTION_STATUSES:
                unresolved.append("CORPORATE_ACTION")
            if unresolved:
                asset["trading_status"] = "UNVERIFIED_" + "_AND_".join(unresolved)
                asset["quality"] = "OPEN_MATCH_BUT_PRIMARY_GATES_NOT_CLEARED"
        if "fund_metadata" in base_asset:
            asset["fund_metadata"] = base_asset["fund_metadata"]
        assets.append(asset)

    as_of = max(quote_times)
    sources = [
        {
            "id": source_id,
            "url": urls[source_id],
            "accessed_at": captured_at.isoformat(),
            "response_sha256": hashlib.sha256(raw_payloads[source_id]).hexdigest(),
            "tier": "C",
        }
        for source_id in source_ids
    ]
    evidence = list(base.get("evidence", [])) + [
        {
            "title": "09:30-09:35双源开盘行情自动封存",
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
        "market_state": "open",
        "simulation_only": True,
        "collection_note": (
            "由capture-open命令在09:30-09:35窗口内自动封存；逐只要求腾讯与新浪开盘价、"
            "上一收盘价、时间戳和非零成交量通过门禁。公司行为状态仅从更早的不可变盘前"
            "快照继承；未清除时即使价格一致也不得结算。"
        ),
        "indices": list(base.get("indices", [])),
        "assets": assets,
        "sources": sources,
        "evidence": evidence,
        "deepseek_usage": {
            "actual_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_cny": 0.0,
        },
    }
    _write_json_exclusive(output_path, snapshot)
    return {
        "status": "SEALED",
        "output": str(output_path),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "as_of": as_of.isoformat(),
        "symbols": symbols,
        "sources": source_ids,
    }
