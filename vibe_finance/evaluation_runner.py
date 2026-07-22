from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .candidate_strategies import (
    CANDIDATE_SYMBOLS,
    DEFAULT_CANDIDATE_MANIFEST,
    build_b1_target,
    build_b2_target,
    build_b3_target,
    load_candidate_protocol,
)
from .evaluation_execution import (
    canonical_sha256,
    plan_frozen_b0_orders,
    plan_rebalance_orders,
    simulate_next_open,
)
from .frozen_baseline import run_frozen_b0_decision


EVIDENCE_CLASS = "TEST_SYNTHETIC_MECHANICAL_ONLY"
COST_SCENARIOS_BPS = (10, 25, 50)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class EvaluationRunnerError(ValueError):
    """A synthetic mechanical replay cannot be proven from its bound input."""


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _money(value: Any) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _read_bound_input(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationRunnerError(f"cannot read synthetic input: {error}") from error
    if not isinstance(payload, dict):
        raise EvaluationRunnerError("synthetic input must be a JSON object")
    try:
        canonical_hash = canonical_sha256(payload)
    except (TypeError, ValueError) as error:
        raise EvaluationRunnerError("synthetic input is not canonical finite JSON") from error
    return payload, {
        "bytes_sha256": _sha256_bytes(raw),
        "canonical_sha256": canonical_hash,
    }


def _iso_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise EvaluationRunnerError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise EvaluationRunnerError(f"{label} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise EvaluationRunnerError(f"{label} must be YYYY-MM-DD")
    return value


def _validate_input(payload: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "evidence_class",
        "initial_cash_cny",
        "readiness_artifact_sha256",
        "trading_calendar_dates",
        "point_in_time_rows",
        "source_registry",
        "cycles",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise EvaluationRunnerError("synthetic input keys missing:" + ",".join(missing))
    if payload.get("schema_version") != 1 or payload.get("evidence_class") != EVIDENCE_CLASS:
        raise EvaluationRunnerError("synthetic input evidence class or schema mismatch")
    cash = payload.get("initial_cash_cny")
    if not isinstance(cash, (int, float)) or isinstance(cash, bool) or cash <= 0:
        raise EvaluationRunnerError("initial_cash_cny must be positive")
    if not _is_sha256(payload.get("readiness_artifact_sha256")):
        raise EvaluationRunnerError("readiness_artifact_sha256 invalid")

    calendar_raw = payload.get("trading_calendar_dates")
    if not isinstance(calendar_raw, list):
        raise EvaluationRunnerError("trading_calendar_dates must be a list")
    calendar = [_iso_date(value, "trading calendar date") for value in calendar_raw]
    if calendar != sorted(set(calendar)):
        raise EvaluationRunnerError("trading calendar must be strictly increasing")

    rows = payload.get("point_in_time_rows")
    if not isinstance(rows, list):
        raise EvaluationRunnerError("point_in_time_rows must be a list")
    row_dates: list[str] = []
    expected_row_keys = {"date", *CANDIDATE_SYMBOLS}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_row_keys:
            raise EvaluationRunnerError("point-in-time row schema mismatch")
        row_dates.append(_iso_date(row.get("date"), "point-in-time row date"))

    source_registry = payload.get("source_registry")
    if not isinstance(source_registry, dict):
        raise EvaluationRunnerError("source_registry must be an object")
    try:
        canonical_sha256(source_registry)
    except (TypeError, ValueError) as error:
        raise EvaluationRunnerError("source_registry is not canonical finite JSON") from error

    cycles = payload.get("cycles")
    if not isinstance(cycles, list) or len(cycles) < 2:
        raise EvaluationRunnerError("at least two synthetic replay cycles are required")
    previous_signal = ""
    required_cycle_keys = {
        "signal_date",
        "execute_date",
        "execution_cutoff",
        "signal_closes",
        "b0_snapshot",
        "open_observations",
    }
    for index, cycle in enumerate(cycles):
        if not isinstance(cycle, dict) or not required_cycle_keys.issubset(cycle):
            raise EvaluationRunnerError(f"synthetic cycle schema mismatch:{index}")
        signal_date = _iso_date(cycle.get("signal_date"), "cycle signal_date")
        execute_date = _iso_date(cycle.get("execute_date"), "cycle execute_date")
        if signal_date <= previous_signal:
            raise EvaluationRunnerError("cycle signal dates must be strictly increasing")
        previous_signal = signal_date
        try:
            signal_index = calendar.index(signal_date)
        except ValueError as error:
            raise EvaluationRunnerError("cycle signal_date not in bound calendar") from error
        if signal_index + 1 >= len(calendar) or calendar[signal_index + 1] != execute_date:
            raise EvaluationRunnerError("cycle execute_date must be the next bound trading date")
        try:
            cutoff = datetime.fromisoformat(
                str(cycle.get("execution_cutoff", "")).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise EvaluationRunnerError("cycle execution_cutoff must be ISO-8601") from error
        if cutoff.tzinfo is None or cutoff.date().isoformat() != execute_date:
            raise EvaluationRunnerError("cycle execution_cutoff must bind the execute date")
        closes = cycle.get("signal_closes")
        if not isinstance(closes, dict) or set(closes) != set(CANDIDATE_SYMBOLS):
            raise EvaluationRunnerError("cycle signal_closes symbol mismatch")
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not Decimal(str(value)).is_finite()
            or value <= 0
            for value in closes.values()
        ):
            raise EvaluationRunnerError("cycle signal_closes must be finite and positive")
        snapshot = cycle.get("b0_snapshot")
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("run_date") != signal_date
            or snapshot.get("market_state") != "closed"
        ):
            raise EvaluationRunnerError("cycle B0 snapshot must be a bound t-close snapshot")
        assets = snapshot.get("assets")
        if not isinstance(assets, list):
            raise EvaluationRunnerError("cycle B0 snapshot assets invalid")
        snapshot_closes = {
            str(asset.get("symbol")): asset.get("close")
            for asset in assets
            if isinstance(asset, dict)
        }
        if set(snapshot_closes) != set(CANDIDATE_SYMBOLS) or any(
            Decimal(str(snapshot_closes[symbol])) != Decimal(str(closes[symbol]))
            for symbol in CANDIDATE_SYMBOLS
        ):
            raise EvaluationRunnerError("cycle B0 snapshot close binding mismatch")
        if any(
            isinstance(asset, dict) and ("open" in asset or "open_source_ids" in asset)
            for asset in assets
        ):
            raise EvaluationRunnerError("t-close B0 snapshot must not contain next-open fields")
        opens = cycle.get("open_observations")
        if not isinstance(opens, dict) or set(opens) != set(CANDIDATE_SYMBOLS):
            raise EvaluationRunnerError("cycle open_observations symbol mismatch")

    last_signal_index = calendar.index(str(cycles[-1]["signal_date"]))
    if row_dates != calendar[: last_signal_index + 1]:
        raise EvaluationRunnerError(
            "point_in_time_rows must equal the bound calendar prefix through the final signal"
        )


def _data_bindings(
    payload: dict[str, Any], point_in_time_rows: list[dict[str, Any]]
) -> dict[str, str]:
    return {
        "readiness_artifact_sha256": str(payload["readiness_artifact_sha256"]),
        "canonical_panel_sha256": canonical_sha256(point_in_time_rows),
        "source_registry_sha256": canonical_sha256(payload["source_registry"]),
        "calendar_sha256": canonical_sha256(payload["trading_calendar_dates"]),
    }


def _bind_position_accounting(
    plan: dict[str, Any], position_accounting: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Add accounting to B1/B2/B3 pre-state while preserving plan integrity checks."""
    bound = copy.deepcopy(plan)
    bound["pre_state"]["position_accounting"] = copy.deepcopy(position_accounting)
    bound["pre_state_sha256"] = canonical_sha256(bound["pre_state"])
    for order in bound["orders"]:
        identity = {
            "candidate": bound.get("candidate"),
            "candidate_proposal_sha256": bound.get("candidate_proposal_sha256"),
            "execution_semantics": bound.get("execution_semantics"),
            "frozen_signal_sha256": bound.get("frozen_signal_sha256"),
            "signal_date": bound.get("signal_date"),
            "execute_date": bound.get("execute_date"),
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": order["quantity"],
            "raw_open_limit": order.get("raw_open_limit"),
            "pre_state_sha256": bound["pre_state_sha256"],
            "protocol_manifest_sha256": bound.get("protocol_manifest_sha256"),
        }
        order["order_id"] = canonical_sha256(identity)
    bound["claim_boundary"]["position_accounting_bound"] = True
    bound.pop("plan_sha256", None)
    bound["plan_sha256"] = canonical_sha256(bound)
    return bound


def _roll_t_plus_one(state: dict[str, Any], signal_date: str) -> dict[str, Any]:
    rolled = copy.deepcopy(state)
    rolled["available_to_sell_quantities"] = {
        symbol: (
            int(rolled["quantities"][symbol])
            if int(rolled["quantities"][symbol]) > 0
            and str(rolled["position_accounting"][symbol]["last_buy_date"])
            < signal_date
            else 0
        )
        for symbol in CANDIDATE_SYMBOLS
    }
    return rolled


def _nav(state: dict[str, Any], closes: dict[str, float]) -> float:
    return _money(
        Decimal(str(state["cash_cny"]))
        + sum(
            Decimal(state["quantities"][symbol]) * Decimal(str(closes[symbol]))
            for symbol in CANDIDATE_SYMBOLS
        )
    )


def _run_scenario(
    *,
    candidate: str,
    slippage_bps: int,
    payload: dict[str, Any],
    repo_root: Path,
    manifest_path: Path,
    prebuilt_signals: dict[str, dict[str, dict[str, Any]]],
    scratch: Path,
) -> dict[str, Any]:
    calendar = payload["trading_calendar_dates"]
    rows = payload["point_in_time_rows"]
    state: dict[str, Any] = {
        "cash_cny": _money(payload["initial_cash_cny"]),
        "quantities": {symbol: 0 for symbol in CANDIDATE_SYMBOLS},
        "available_to_sell_quantities": {symbol: 0 for symbol in CANDIDATE_SYMBOLS},
        "position_accounting": {},
        "cumulative_realized_pnl_cny": 0.0,
    }
    trace: list[dict[str, Any]] = []
    for cycle_index, cycle in enumerate(payload["cycles"]):
        signal_date = str(cycle["signal_date"])
        execute_date = str(cycle["execute_date"])
        signal_index = calendar.index(signal_date)
        point_in_time_rows = rows[: signal_index + 1]
        bindings = _data_bindings(payload, point_in_time_rows)
        closes = {symbol: float(cycle["signal_closes"][symbol]) for symbol in CANDIDATE_SYMBOLS}
        state = _roll_t_plus_one(state, signal_date)
        nav_cny = _nav(state, closes)

        if candidate == "B0":
            snapshot_path = scratch / f"b0-{slippage_bps}-{cycle_index:03d}.json"
            snapshot_path.write_text(
                json.dumps(
                    cycle["b0_snapshot"],
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            signal = run_frozen_b0_decision(
                repo_root,
                manifest_path,
                snapshot_path,
                {
                    "cash_cny": state["cash_cny"],
                    "positions": copy.deepcopy(state["position_accounting"]),
                },
                trading_calendar_dates=calendar,
                data_bindings=bindings,
            )
            plan = plan_frozen_b0_orders(
                frozen_signal=signal,
                execute_date=execute_date,
                trading_calendar_dates=calendar,
                data_bindings=bindings,
                current_cash_cny=state["cash_cny"],
                current_quantities=state["quantities"],
                available_to_sell_quantities=state[
                    "available_to_sell_quantities"
                ],
                signal_closes=closes,
                nav_cny=nav_cny,
                position_accounting=state["position_accounting"],
            )
        else:
            signal = copy.deepcopy(prebuilt_signals[candidate][signal_date])
            plan = plan_rebalance_orders(
                candidate_signal=signal,
                execute_date=execute_date,
                trading_calendar_dates=calendar,
                data_bindings=bindings,
                current_cash_cny=state["cash_cny"],
                current_quantities=state["quantities"],
                available_to_sell_quantities=state[
                    "available_to_sell_quantities"
                ],
                signal_closes=closes,
                nav_cny=nav_cny,
            )
            plan = _bind_position_accounting(plan, state["position_accounting"])

        if plan.get("claim_boundary", {}).get("next_open_observed") is not False:
            raise EvaluationRunnerError("planning observed a next-open value")
        execution = simulate_next_open(
            plan,
            open_observations=cycle["open_observations"],
            source_registry=payload["source_registry"],
            execution_cutoff=str(cycle["execution_cutoff"]),
            cash_cny=state["cash_cny"],
            current_quantities=state["quantities"],
            available_to_sell_quantities=state["available_to_sell_quantities"],
            slippage_bps=slippage_bps,
            position_accounting=state["position_accounting"],
        )
        realized = _money(execution["after_state"].get("realized_pnl_cny", 0.0))
        cumulative_realized = _money(
            Decimal(str(state["cumulative_realized_pnl_cny"]))
            + Decimal(str(realized))
        )
        state = {
            "cash_cny": execution["after_state"]["cash_cny"],
            "quantities": execution["after_state"]["quantities"],
            "available_to_sell_quantities": execution["after_state"][
                "available_to_sell_quantities"
            ],
            "position_accounting": execution["after_state"][
                "position_accounting"
            ],
            "cumulative_realized_pnl_cny": cumulative_realized,
        }
        trace.append(
            {
                "evidence_class": EVIDENCE_CLASS,
                "signal_date": signal_date,
                "execute_date": execute_date,
                "signal_input_panel_sha256": bindings["canonical_panel_sha256"],
                "signal": signal,
                "plan": plan,
                "execution": execution,
                "state_after": copy.deepcopy(state),
                "metrics": None,
                "ranking": None,
                "promotion_authorized": False,
            }
        )
    return {
        "evidence_class": EVIDENCE_CLASS,
        "slippage_bps": slippage_bps,
        "cycles": trace,
        "final_state": state,
        "metrics": None,
        "ranking": None,
        "promotion_authorized": False,
    }


def run_synthetic_mechanical_evaluation(
    input_path: Path,
    *,
    repo_root: Path = REPOSITORY_ROOT,
    manifest_path: Path = DEFAULT_CANDIDATE_MANIFEST,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Replay B0/B1/B2/B3 over bound synthetic days; never compute performance."""
    if output_path is not None and output_path.exists():
        raise EvaluationRunnerError(f"synthetic artifact already exists: {output_path}")
    payload, input_binding = _read_bound_input(input_path)
    _validate_input(payload)
    protocol = load_candidate_protocol(manifest_path)
    if protocol.manifest_sha256 != load_candidate_protocol(
        DEFAULT_CANDIDATE_MANIFEST
    ).manifest_sha256:
        raise EvaluationRunnerError("runner manifest differs from the code-bound protocol")

    calendar = payload["trading_calendar_dates"]
    rows = payload["point_in_time_rows"]
    prebuilt_signals: dict[str, dict[str, dict[str, Any]]] = {
        "B1": {},
        "B2": {},
        "B3": {},
    }
    for cycle in payload["cycles"]:
        signal_date = str(cycle["signal_date"])
        point_in_time_rows = rows[: calendar.index(signal_date) + 1]
        prebuilt_signals["B1"][signal_date] = build_b1_target(
            point_in_time_rows,
            trading_calendar_dates=calendar,
            signal_date=signal_date,
        )
        prebuilt_signals["B2"][signal_date] = build_b2_target(
            point_in_time_rows,
            trading_calendar_dates=calendar,
            signal_date=signal_date,
        )
        prebuilt_signals["B3"][signal_date] = build_b3_target(
            point_in_time_rows,
            trading_calendar_dates=calendar,
            signal_date=signal_date,
        )

    candidates: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="vibe-synthetic-evaluation-", dir="/tmp") as directory:
        scratch = Path(directory)
        for candidate in ("B0", "B1", "B2", "B3"):
            candidates[candidate] = {
                "evidence_class": EVIDENCE_CLASS,
                "metrics": None,
                "ranking": None,
                "promotion_authorized": False,
                "cost_scenarios": {
                    str(slippage_bps): _run_scenario(
                        candidate=candidate,
                        slippage_bps=slippage_bps,
                        payload=payload,
                        repo_root=repo_root,
                        manifest_path=manifest_path,
                        prebuilt_signals=prebuilt_signals,
                        scratch=scratch,
                    )
                    for slippage_bps in COST_SCENARIOS_BPS
                },
            }

    result = {
        "schema_version": 1,
        "status": "PASS_TEST_SYNTHETIC_MECHANICAL_CLOSED_LOOP",
        "evidence_class": EVIDENCE_CLASS,
        "protocol_manifest_sha256": protocol.manifest_sha256,
        "input_binding": input_binding,
        "cost_scenarios_bps": list(COST_SCENARIOS_BPS),
        "candidates": candidates,
        "metrics": None,
        "ranking": None,
        "strategy_ranking": None,
        "promotion_authorized": False,
        "claim_boundary": {
            "synthetic_only": True,
            "mechanical_replay_only": True,
            "entry_run_save_reload_eval_supported": True,
            "next_open_excluded_from_signal_and_plan_inputs": True,
            "real_market_performance_computed": False,
            "strategy_ranking_computed": False,
            "promotion_authorized": False,
        },
    }
    result["artifact_sha256"] = canonical_sha256(result)
    if output_path is not None:
        write_synthetic_evaluation_artifact(output_path, result)
    return result


def write_synthetic_evaluation_artifact(
    path: Path, artifact: dict[str, Any]
) -> None:
    """Atomically save one immutable, self-hashed synthetic replay artifact."""
    if path.exists():
        raise EvaluationRunnerError(f"synthetic artifact already exists: {path}")
    claimed = artifact.get("artifact_sha256")
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    if not _is_sha256(claimed) or canonical_sha256(unsigned) != claimed:
        raise EvaluationRunnerError("synthetic artifact hash mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            artifact,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def verify_synthetic_evaluation_artifact(
    input_path: Path,
    artifact_path: Path,
    *,
    repo_root: Path = REPOSITORY_ROOT,
    manifest_path: Path = DEFAULT_CANDIDATE_MANIFEST,
) -> dict[str, Any]:
    """Reload, validate bindings, rerun, and compare a saved synthetic artifact."""
    mismatches: list[str] = []
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationRunnerError(f"cannot read synthetic artifact: {error}") from error
    if not isinstance(artifact, dict):
        raise EvaluationRunnerError("synthetic artifact must be a JSON object")
    claimed = artifact.get("artifact_sha256")
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    if not _is_sha256(claimed) or canonical_sha256(unsigned) != claimed:
        mismatches.append("ARTIFACT_HASH_MISMATCH")
    try:
        _, input_binding = _read_bound_input(input_path)
    except EvaluationRunnerError as error:
        mismatches.append(f"INPUT_READ_FAILED:{error}")
        input_binding = None
    if input_binding is not None and input_binding != artifact.get("input_binding"):
        mismatches.append("INPUT_CONTENT_HASH_MISMATCH")
    replay: dict[str, Any] | None = None
    if not mismatches:
        try:
            replay = run_synthetic_mechanical_evaluation(
                input_path,
                repo_root=repo_root,
                manifest_path=manifest_path,
            )
        except (ValueError, OSError) as error:
            mismatches.append(f"REPLAY_FAILED:{error}")
        else:
            if replay.get("artifact_sha256") != claimed:
                mismatches.append("REPLAY_ARTIFACT_MISMATCH")
    return {
        "schema_version": 1,
        "status": (
            "VERIFIED_TEST_SYNTHETIC_MECHANICAL_CLOSED_LOOP"
            if not mismatches
            else "INVALID"
        ),
        "evidence_class": EVIDENCE_CLASS,
        "artifact_path": str(artifact_path),
        "artifact_file_sha256": _sha256_bytes(artifact_path.read_bytes()),
        "mismatches": mismatches,
        "replayed_artifact_sha256": replay.get("artifact_sha256") if replay else None,
        "metrics": None,
        "ranking": None,
        "strategy_ranking": None,
        "promotion_authorized": False,
        "claim_boundary": {
            "entry_run_save_reload_eval_verified": not mismatches,
            "real_market_performance_verified": False,
            "promotion_authorized": False,
        },
    }
