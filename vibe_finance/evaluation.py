from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVALUATION_MANIFEST = REPOSITORY_ROOT / "config/evaluation/b0-b1-b2-v1.json"
DEFAULT_TRADING_CALENDAR = REPOSITORY_ROOT / "config/evaluation/trading-calendar.json"
DEFAULT_SOURCES = REPOSITORY_ROOT / "config/sources.json"
DEFAULT_UNIVERSE = REPOSITORY_ROOT / "config/universe.json"
TRUSTED_POINT_IN_TIME_PANEL_LOADER_AVAILABLE = False
CODE_APPROVED_PROTOCOL_SHA256 = {
    "manifest": "5679ca06263a22243fc6f06852d2832f0ebac4dc8f4de16979976f2e35fa688e",
    "sources": "a82bbe176be533f889fcd2807626ce6d17078543515660cf023efbb00d45f073",
    "universe": "23a8197aa3e9166949f669070318422861d77e3ff3d8af1da12be3ebaf7d29b7",
    "calendar": "d5228970b61e60acf11cdd9dcacf91eb4f185bd8dc7647d3a76a30003d289abf",
}


class EvaluationDataError(ValueError):
    """Raised when a strategy-evaluation dataset violates point-in-time rules."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationDataError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvaluationDataError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_walk_forward_splits(
    trading_dates: list[str],
    *,
    development_window: int = 756,
    validation_window: int = 252,
    test_window: int = 126,
    step: int = 126,
    sealed_oos_window: int = 504,
    maximum_forward_label_horizon: int,
) -> dict[str, Any]:
    """Freeze deterministic common-calendar development folds and sealed OOS."""
    if maximum_forward_label_horizon < 0:
        raise EvaluationDataError("maximum_forward_label_horizon must be nonnegative")
    if any(value <= 0 for value in (development_window, validation_window, test_window, step, sealed_oos_window)):
        raise EvaluationDataError("split windows and step must be positive")
    parsed = [_parse_date(value, "trading_date") for value in trading_dates]
    ordered = [value.isoformat() for value in sorted(parsed)]
    if len(ordered) != len(set(ordered)):
        raise EvaluationDataError("DUPLICATE_TRADING_DATE")
    required = development_window + validation_window + test_window + sealed_oos_window
    if len(ordered) < required:
        raise EvaluationDataError(
            f"INSUFFICIENT_DISTINCT_TRADING_DATES:{len(ordered)}:{required}"
        )
    development_dates = ordered[:-sealed_oos_window]
    sealed_oos = ordered[-sealed_oos_window:]
    scored_window = development_window + validation_window + test_window
    folds: list[dict[str, Any]] = []
    start = 0
    while start + scored_window <= len(development_dates):
        train_end = start + development_window
        validation_end = train_end + validation_window
        test_end = validation_end + test_window
        purge = maximum_forward_label_horizon
        train_score_end = max(start, train_end - purge)
        validation_score_end = max(train_end, validation_end - purge)
        folds.append(
            {
                "fold": len(folds),
                "train": development_dates[start:train_score_end],
                "train_purged": development_dates[train_score_end:train_end],
                "validation": development_dates[train_end:validation_score_end],
                "validation_purged": development_dates[validation_score_end:validation_end],
                "test": development_dates[validation_end:test_end],
            }
        )
        start += step
    if not folds:
        raise EvaluationDataError("NO_WALK_FORWARD_FOLDS")
    return {
        "schema_version": 1,
        "common_trading_date_count": len(ordered),
        "development_date_count": len(development_dates),
        "sealed_oos": {
            "date_count": len(sealed_oos),
            "ordered_dates": sealed_oos,
            "ordered_dates_sha256": _canonical_sha256(sealed_oos),
        },
        "maximum_forward_label_horizon": maximum_forward_label_horizon,
        "folds": folds,
    }


def write_readiness_artifact(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise EvaluationDataError(f"readiness artifact is immutable and already exists: {path}")
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
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


def verify_readiness_artifact(path: Path) -> dict[str, Any]:
    artifact = _read_object(path)
    bindings = artifact.get("bindings")
    if not isinstance(bindings, dict):
        raise EvaluationDataError("readiness artifact has no bindings")
    mismatches: list[str] = []
    if artifact.get("authority") != "PRODUCTION_PROTECTED_PROTOCOL":
        mismatches.append("ARTIFACT_AUTHORITY_NOT_PRODUCTION_PROTECTED")
    for name in ("manifest", "sources", "universe", "calendar"):
        binding = bindings.get(name)
        if not isinstance(binding, dict):
            mismatches.append(f"BINDING_MISSING:{name}")
            continue
        bound_path = Path(str(binding.get("path", "")))
        if not bound_path.is_file():
            mismatches.append(f"BOUND_FILE_MISSING:{name}")
        elif _sha256(bound_path) != binding.get("sha256"):
            mismatches.append(f"BOUND_FILE_HASH_MISMATCH:{name}")
    input_paths: list[Path] = []
    requested_inputs = artifact.get("dataset", {}).get("requested_inputs", [])
    if not isinstance(requested_inputs, list):
        mismatches.append("REQUESTED_INPUT_BINDINGS_MISSING")
        requested_inputs = []
    for item in requested_inputs:
        bound_path = Path(str(item.get("path", "")))
        input_paths.append(bound_path)
        if not bound_path.is_file():
            mismatches.append(f"INPUT_MISSING:{bound_path}")
        elif _sha256(bound_path) != item.get("sha256"):
            mismatches.append(f"INPUT_HASH_MISMATCH:{bound_path}")
    recomputed: dict[str, Any] | None = None
    if not mismatches:
        try:
            manifest_binding = bindings["manifest"]
            if Path(str(manifest_binding["path"])).resolve() != DEFAULT_EVALUATION_MANIFEST.resolve():
                mismatches.append("PROTECTED_MANIFEST_PATH_MISMATCH")
            else:
                recomputed = audit_evaluation_readiness(
                    input_paths,
                    sources_path=Path(str(bindings["sources"]["path"])),
                    universe_path=Path(str(bindings["universe"]["path"])),
                    manifest_path=Path(str(bindings["manifest"]["path"])),
                    calendar_path=Path(str(bindings["calendar"]["path"])),
                )
                if _canonical_sha256(recomputed) != _canonical_sha256(artifact):
                    mismatches.append("FULL_READINESS_RECOMPUTATION_MISMATCH")
        except (EvaluationDataError, KeyError, OSError, TypeError, ValueError) as error:
            mismatches.append(f"READINESS_RECOMPUTATION_FAILED:{error}")
    if mismatches:
        status = "INVALID"
    elif recomputed and recomputed.get("status") == "READY_FOR_MECHANICAL_EVALUATION":
        status = "VERIFIED_READY"
    else:
        status = "VERIFIED_NOT_EVALUABLE"
    return {
        "schema_version": 1,
        "status": status,
        "artifact_path": str(path),
        "artifact_sha256": _sha256(path),
        "mismatches": mismatches,
        "recomputed_readiness_status": recomputed.get("status") if recomputed else None,
        "claim_boundary": {
            "strategy_returns_verified": False,
            "promotion_authorized": False,
        },
    }


def _parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise EvaluationDataError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise EvaluationDataError(f"{label} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise EvaluationDataError(f"{label} must be YYYY-MM-DD")
    return parsed


def _parse_time(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise EvaluationDataError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvaluationDataError(f"{label} must include a timezone")
    return parsed


def audit_evaluation_readiness(
    input_paths: list[Path],
    *,
    sources_path: Path,
    universe_path: Path,
    manifest_path: Path = DEFAULT_EVALUATION_MANIFEST,
    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
    minimum_unique_dates: int | None = None,
    test_only_allow_unprotected_protocol: bool = False,
) -> dict[str, Any]:
    """Audit whether immutable snapshots can support WF plus independent OOS.

    This function never computes strategy returns. It only proves or rejects the
    dataset contract needed before B0/B1/B2 may be evaluated.
    """
    protected_paths = {
        "manifest": DEFAULT_EVALUATION_MANIFEST.resolve(),
        "sources": DEFAULT_SOURCES.resolve(),
        "universe": DEFAULT_UNIVERSE.resolve(),
        "calendar": DEFAULT_TRADING_CALENDAR.resolve(),
    }
    supplied_paths = {
        "manifest": manifest_path.resolve(),
        "sources": sources_path.resolve(),
        "universe": universe_path.resolve(),
        "calendar": calendar_path.resolve(),
    }
    if not test_only_allow_unprotected_protocol:
        mismatched = [
            name for name in protected_paths if supplied_paths[name] != protected_paths[name]
        ]
        if mismatched:
            raise EvaluationDataError(
                "production readiness protocol path mismatch: " + ",".join(mismatched)
            )
        content_mismatched = [
            name
            for name, path in supplied_paths.items()
            if _sha256(path) != CODE_APPROVED_PROTOCOL_SHA256[name]
        ]
        if content_mismatched:
            raise EvaluationDataError(
                "production readiness protocol content hash mismatch: "
                + ",".join(content_mismatched)
            )
    authority = (
        "TEST_ONLY_UNPROTECTED_PROTOCOL"
        if test_only_allow_unprotected_protocol
        else "PRODUCTION_PROTECTED_PROTOCOL"
    )
    manifest = _read_object(manifest_path)
    calendar = _read_object(calendar_path)
    calendar_dates_raw = calendar.get("dates", [])
    if not isinstance(calendar_dates_raw, list):
        raise EvaluationDataError("calendar.dates must be a list")
    try:
        calendar_dates = [_parse_date(value, "calendar.date").isoformat() for value in calendar_dates_raw]
    except EvaluationDataError:
        raise
    if calendar_dates != sorted(set(calendar_dates)):
        raise EvaluationDataError("calendar dates must be unique and strictly increasing")
    calendar_date_set = set(calendar_dates)
    dataset_contract = manifest.get("dataset_contract", {})
    preregistered_minimum = int(
        dataset_contract.get("minimum_common_history_trading_days", 0)
    )
    if preregistered_minimum < 3:
        raise EvaluationDataError(
            "manifest minimum_common_history_trading_days must be at least 3"
        )
    if minimum_unique_dates is not None and minimum_unique_dates < preregistered_minimum:
        raise EvaluationDataError(
            "minimum_unique_dates cannot relax the preregistered manifest"
        )
    required_unique_dates = max(
        preregistered_minimum, minimum_unique_dates or preregistered_minimum
    )
    sources = _read_object(sources_path)
    universe = _read_object(universe_path)
    registry = {
        str(item.get("id")): item
        for item in sources.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    reasons: set[str] = set()
    warnings: set[str] = set()
    schema_counts: dict[str, int] = defaultdict(int)
    symbol_dates: dict[str, set[str]] = defaultdict(set)
    source_ids_seen: set[str] = set()
    unregistered_sources: set[str] = set()
    excluded_inputs: list[str] = []
    accepted_inputs: list[dict[str, Any]] = []
    seen_panel_keys: set[tuple[str, str]] = set()
    required_fields = set(dataset_contract.get("point_in_time_fields", []))
    candidate_symbols = {
        str(symbol)
        for candidate_id in ("B1", "B2")
        for symbol in manifest.get("candidates", {}).get(candidate_id, {}).get("symbols", [])
    }
    if calendar.get("status") != "VERIFIED_POINT_IN_TIME_TRADING_CALENDAR":
        reasons.add("AUTHORITATIVE_TRADING_CALENDAR_UNAVAILABLE")
    if not TRUSTED_POINT_IN_TIME_PANEL_LOADER_AVAILABLE and not test_only_allow_unprotected_protocol:
        reasons.add("TRUSTED_POINT_IN_TIME_PANEL_LOADER_NOT_IMPLEMENTED")

    if not input_paths:
        reasons.add("NO_INPUT_SNAPSHOTS")
    for path in sorted(input_paths):
        lowered = path.name.lower()
        snapshot = _read_object(path)
        provenance = snapshot.get("provenance", {})
        origin = provenance.get("origin") if isinstance(provenance, dict) else None
        if "recovery" in lowered or origin in {"recovery", "manual_patch"}:
            excluded_inputs.append(str(path))
            warnings.add(f"RECOVERY_SNAPSHOT_EXCLUDED:{path.name}")
            continue
        if origin != "canonical":
            reasons.add(f"SNAPSHOT_ORIGIN_NOT_CANONICAL:{path.name}")
        schema_version = snapshot.get("schema_version")
        schema_counts[str(schema_version)] += 1
        if schema_version != 2:
            reasons.add(f"SNAPSHOT_SCHEMA_NOT_V2:{path.name}")
        run_date = _parse_date(snapshot.get("run_date"), f"{path.name}.run_date")
        if run_date.isoformat() not in calendar_date_set:
            reasons.add(f"RUN_DATE_NOT_IN_AUTHORITATIVE_CALENDAR:{path.name}")
        if snapshot.get("is_trading_day") is not True:
            reasons.add(f"SNAPSHOT_NOT_TRADING_DAY:{path.name}")
        if snapshot.get("market_state") != "closed":
            reasons.add(f"SNAPSHOT_NOT_CLOSED_CUTOFF:{path.name}")
        cutoff = _parse_time(snapshot.get("as_of"), f"{path.name}.as_of")
        if cutoff.date() > run_date:
            reasons.add(f"SNAPSHOT_AS_OF_AFTER_RUN_DATE:{path.name}")
        assets = snapshot.get("assets")
        if not isinstance(assets, list):
            raise EvaluationDataError(f"{path.name}.assets must be a list")
        accepted_inputs.append(
            {
                "path": str(path),
                "sha256": _sha256(path),
                "run_date": run_date.isoformat(),
                "as_of": cutoff.isoformat(),
            }
        )
        for asset in assets:
            if not isinstance(asset, dict):
                raise EvaluationDataError(f"{path.name}.assets contains a non-object")
            symbol = str(asset.get("symbol", ""))
            if not symbol:
                raise EvaluationDataError(f"{path.name} contains an asset without symbol")
            panel_key = (run_date.isoformat(), symbol)
            if panel_key in seen_panel_keys:
                reasons.add(f"DUPLICATE_CANONICAL_PANEL_ROW:{run_date}:{symbol}")
            seen_panel_keys.add(panel_key)
            for field in sorted(required_fields - {"date", "symbol"}):
                if field not in asset:
                    reasons.add(f"POINT_IN_TIME_FIELD_MISSING:{symbol}:{field}:{path.name}")
            for field in ("raw_open", "raw_close", "total_return_close"):
                try:
                    numeric = float(asset.get(field))
                except (TypeError, ValueError):
                    numeric = float("nan")
                if not (numeric > 0.0 and numeric < float("inf")):
                    reasons.add(f"PRICE_FIELD_INVALID:{symbol}:{field}:{path.name}")
            if not isinstance(asset.get("corporate_actions"), list):
                reasons.add(f"CORPORATE_ACTIONS_NOT_LIST:{symbol}:{path.name}")
            snapshot_sha = asset.get("snapshot_sha256")
            if (
                not isinstance(snapshot_sha, str)
                or len(snapshot_sha) != 64
                or any(character not in "0123456789abcdef" for character in snapshot_sha.lower())
            ):
                reasons.add(f"SNAPSHOT_SHA256_INVALID:{symbol}:{path.name}")

            close_observed_value = asset.get("close_observed_at")
            if not close_observed_value:
                reasons.add(f"CLOSE_OBSERVED_AT_MISSING:{symbol}:{path.name}")
            else:
                observed = _parse_time(
                    close_observed_value, f"{path.name}.{symbol}.close_observed_at"
                )
                if observed > cutoff.astimezone(observed.tzinfo):
                    reasons.add(f"CLOSE_AVAILABLE_AFTER_CUTOFF:{symbol}:{path.name}")
                elif observed.date() != run_date:
                    reasons.add(f"CLOSE_DATE_MISMATCH:{symbol}:{path.name}")
                else:
                    symbol_dates[symbol].add(run_date.isoformat())
            open_observed_value = asset.get("open_observed_at")
            if not open_observed_value:
                reasons.add(f"OPEN_OBSERVED_AT_MISSING:{symbol}:{path.name}")
            else:
                open_observed = _parse_time(
                    open_observed_value, f"{path.name}.{symbol}.open_observed_at"
                )
                if open_observed > cutoff.astimezone(open_observed.tzinfo):
                    reasons.add(f"OPEN_AVAILABLE_AFTER_CUTOFF:{symbol}:{path.name}")
                elif open_observed.date() != run_date:
                    reasons.add(f"OPEN_DATE_MISMATCH:{symbol}:{path.name}")

            price_sources: list[Any] = []
            for role in ("open_source_ids", "close_source_ids"):
                role_sources = asset.get(role)
                if not isinstance(role_sources, list) or len(set(role_sources)) < 2:
                    reasons.add(f"PRICE_SOURCE_ROLE_INSUFFICIENT:{symbol}:{role}:{path.name}")
                else:
                    price_sources.extend(role_sources)
                    independence_groups = {
                        registry.get(str(source_id), {}).get("independence_group")
                        for source_id in role_sources
                        if registry.get(str(source_id), {}).get("independence_group")
                    }
                    if len(independence_groups) < 2:
                        reasons.add(
                            f"PRICE_SOURCES_NOT_INDEPENDENT:{symbol}:{role}:{path.name}"
                        )
            all_sources = asset.get("source_ids", [])
            if not isinstance(all_sources, list):
                all_sources = []
            for source_id in set(price_sources) | set(all_sources):
                source_id = str(source_id)
                source_ids_seen.add(source_id)
                if source_id not in registry:
                    unregistered_sources.add(source_id)
            history = asset.get("history", [])
            if history:
                reasons.add(f"HISTORY_POINTS_LACK_POINT_IN_TIME_METADATA:{symbol}")

    for source_id in sorted(source_ids_seen):
        item = registry.get(source_id)
        if item is None:
            continue
        if item.get("license_status") not in {
            "PERMITTED_RESEARCH",
            "PERMITTED_INTERNAL_RESEARCH",
            "OPEN_LICENSE",
        }:
            reasons.add(f"SOURCE_LICENSE_NOT_VERIFIED:{source_id}")
        allowed_uses = item.get("allowed_uses")
        if not isinstance(allowed_uses, list) or "local_research_evaluation" not in allowed_uses:
            reasons.add(f"SOURCE_ALLOWED_USE_MISSING:{source_id}")
        if not item.get("independence_group"):
            reasons.add(f"SOURCE_INDEPENDENCE_GROUP_MISSING:{source_id}")
        license_sha = item.get("license_snapshot_sha256")
        if (
            not isinstance(license_sha, str)
            or len(license_sha) != 64
            or any(character not in "0123456789abcdef" for character in license_sha.lower())
        ):
            reasons.add(f"SOURCE_LICENSE_SNAPSHOT_MISSING:{source_id}")
        for field in (
            "access_method",
            "usage_boundary",
            "fallback",
            "last_success_at",
        ):
            if not item.get(field):
                reasons.add(f"SOURCE_GOVERNANCE_FIELD_MISSING:{source_id}:{field}")
    for source_id in unregistered_sources:
        reasons.add(f"UNREGISTERED_SOURCE:{source_id}")

    universe_symbols = {
        str(item.get("symbol"))
        for key in ("listed_funds", "open_end_funds")
        for item in universe.get(key, [])
        if isinstance(item, dict) and item.get("symbol")
    }
    observed_symbols = set(symbol_dates)
    candidates_missing_from_universe = sorted(candidate_symbols - universe_symbols)
    if candidates_missing_from_universe:
        reasons.add(
            "EVALUATION_SYMBOLS_NOT_IN_BOUND_UNIVERSE:"
            + ",".join(candidates_missing_from_universe)
        )
    missing_evaluation_symbols = sorted(candidate_symbols - observed_symbols)
    if missing_evaluation_symbols:
        reasons.add(
            "EVALUATION_SYMBOLS_WITHOUT_ELIGIBLE_OBSERVATIONS:"
            + ",".join(missing_evaluation_symbols)
        )
    common_dates = (
        set.intersection(*(symbol_dates[symbol] for symbol in sorted(candidate_symbols)))
        if candidate_symbols and all(symbol in symbol_dates for symbol in candidate_symbols)
        else set()
    )
    if common_dates:
        first_index = calendar_dates.index(min(common_dates))
        last_index = calendar_dates.index(max(common_dates))
        missing_common_dates = set(calendar_dates[first_index : last_index + 1]) - common_dates
        if missing_common_dates:
            reasons.add(f"COMMON_PANEL_CALENDAR_GAPS:{len(missing_common_dates)}")
    minimum_observed = len(common_dates)
    if minimum_observed < required_unique_dates:
        reasons.add(
            f"MINIMUM_COMMON_UNIQUE_DATES_{minimum_observed}_LT_{required_unique_dates}"
        )

    split_summary: dict[str, Any] | None = None
    if minimum_observed >= required_unique_dates:
        split_config = manifest.get("splits", {})
        walk_forward = split_config.get("walk_forward", {})
        independent_oos = split_config.get("independent_oos", {})
        try:
            splits = build_walk_forward_splits(
                sorted(common_dates),
                development_window=int(walk_forward["development_window_trading_days"]),
                validation_window=int(walk_forward["validation_window_trading_days"]),
                test_window=int(walk_forward["test_window_trading_days"]),
                step=int(walk_forward["step_trading_days"]),
                sealed_oos_window=int(independent_oos["sealed_tail_trading_days"]),
                maximum_forward_label_horizon=int(
                    split_config["maximum_forward_label_horizon_trading_days"]
                ),
            )
        except (EvaluationDataError, KeyError, TypeError, ValueError) as error:
            reasons.add(f"SPLIT_CONTRACT_INVALID:{error}")
        else:
            split_summary = {
                "fold_count": len(splits["folds"]),
                "development_date_count": splits["development_date_count"],
                "sealed_oos_date_count": splits["sealed_oos"]["date_count"],
                "sealed_oos_dates_sha256": splits["sealed_oos"]["ordered_dates_sha256"],
            }

    if reasons:
        status = "NOT_EVALUABLE"
    elif test_only_allow_unprotected_protocol:
        status = "TEST_ONLY_READY_FOR_MECHANICAL_EVALUATION"
    else:
        status = "READY_FOR_MECHANICAL_EVALUATION"
    return {
        "schema_version": 1,
        "authority": authority,
        "status": status,
        "claim_boundary": {
            "strategy_returns_computed": False,
            "walk_forward_computed": False,
            "independent_oos_computed": False,
            "promotion_authorized": False,
        },
        "dataset": {
            "accepted_input_count": len(accepted_inputs),
            "excluded_input_count": len(excluded_inputs),
            "schema_counts": dict(sorted(schema_counts.items())),
            "observed_symbol_count": len(observed_symbols),
            "universe_symbol_count": len(universe_symbols),
            "evaluation_symbol_count": len(candidate_symbols),
            "evaluation_symbols": sorted(candidate_symbols),
            "minimum_unique_dates_per_observed_symbol": minimum_observed,
            "required_common_unique_dates": required_unique_dates,
            "source_registry_sha256": _sha256(sources_path),
            "universe_sha256": _sha256(universe_path),
            "trading_calendar_sha256": _sha256(calendar_path),
            "inputs": accepted_inputs,
            "requested_inputs": [
                {"path": str(path), "sha256": _sha256(path)}
                for path in sorted(input_paths)
            ],
            "excluded_inputs": excluded_inputs,
            "split_summary": split_summary,
        },
        "bindings": {
            "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
            "sources": {"path": str(sources_path), "sha256": _sha256(sources_path)},
            "universe": {"path": str(universe_path), "sha256": _sha256(universe_path)},
            "calendar": {"path": str(calendar_path), "sha256": _sha256(calendar_path)},
        },
        "schema": {
            "required_snapshot_version": 2,
            "required_price_fields": sorted(required_fields),
            "point_in_time_rule": "open_observed_at and close_observed_at <= snapshot.as_of",
            "minimum_independent_price_sources": 2,
        },
        "leakage_risks": [
            "recovery snapshots can contain post-failure information",
            "undated history arrays cannot prove historical availability",
            "current-universe backfill can create survivorship bias",
            "revised prices or NAV without supersedes metadata can leak corrections",
        ],
        "data_card": {
            "license": "VERIFIED_PER_SOURCE_OR_UNKNOWN",
            "provenance": "POINT_LEVEL_REQUIRED",
            "bias_notes": [
                "universe selection bias",
                "duplicate same-day snapshot risk",
                "corporate-action adjustment risk",
                "publication-time leakage risk",
            ],
            "allowed_use": "mechanical evaluation only when status is READY",
            "forbidden_use": "performance claims or strategy promotion when NOT_EVALUABLE",
        },
        "reasons": sorted(reasons),
        "warnings": sorted(warnings),
        "next_steps": [
            "register source license and operational governance fields",
            "store dated observations with available_at and immutable raw hashes",
            "exclude recovery snapshots from signal history",
            "pre-register train, walk-forward, embargo, and independent OOS dates",
        ],
    }
