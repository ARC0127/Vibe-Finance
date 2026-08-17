from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .file_lock import advisory_file_lock, fsync_directory


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIENCE_ROOT = REPO_ROOT / "reports/evolution"
DEFAULT_MODE_LOCK = REPO_ROOT / "MODE_LOCK.json"
DEFAULT_STRATEGY = REPO_ROOT / "config/strategy.json"

EXPERIENCE_ID = re.compile(r"^EXP-[A-Z0-9][A-Z0-9-]{5,63}$")
VALIDATION_STATUSES = {
    "OBSERVED",
    "HYPOTHESIS",
    "TESTING",
    "SUPPORTED",
    "INVALIDATED",
}
STRATEGY_IMPACT_STATUSES = {"NONE", "PROPOSED_ONLY"}
COUNTEREVIDENCE_SEARCH_STATUSES = {
    "FOUND",
    "SEARCHED_NONE_FOUND",
    "NOT_YET_SEARCHED",
}
WRITER_TASKS = {"reflection-evolution", "experience-ledger-bootstrap"}


class ExperienceLedgerError(ValueError):
    """Raised when the project-owned investment experience ledger is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExperienceLedgerError(f"cannot read experience JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ExperienceLedgerError(f"experience JSON must be an object: {path}")
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperienceLedgerError(f"{field} must be a non-empty string")
    return value.strip()


def _require_timezone_timestamp(value: Any, field: str) -> str:
    rendered = _require_text(value, field)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExperienceLedgerError(f"{field} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ExperienceLedgerError(f"{field} must include a timezone")
    return rendered


def _safe_repo_file(repo_root: Path, relative: Any) -> tuple[Path, str]:
    rendered = _require_text(relative, "evidence.path")
    candidate = Path(rendered)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ExperienceLedgerError("evidence path must be repository-relative")
    root = repo_root.resolve()
    unresolved = root / candidate
    if unresolved.is_symlink():
        raise ExperienceLedgerError(f"evidence path must not be a symlink: {rendered}")
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError as error:
        raise ExperienceLedgerError(f"evidence path does not exist: {rendered}") from error
    if root != resolved and root not in resolved.parents:
        raise ExperienceLedgerError(f"evidence path escapes repository: {rendered}")
    if not resolved.is_file():
        raise ExperienceLedgerError(f"evidence path is not a file: {rendered}")
    return resolved, candidate.as_posix()


def _validate_evidence_items(
    value: Any,
    *,
    field: str,
    repo_root: Path,
    allow_empty: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ExperienceLedgerError(f"{field} must be {qualifier}")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ExperienceLedgerError(f"{field}[{index}] must be an object")
        path, relative = _safe_repo_file(repo_root, item.get("path"))
        expected = _require_text(item.get("sha256"), f"{field}[{index}].sha256")
        actual = sha256_file(path)
        if actual != expected:
            raise ExperienceLedgerError(f"{field}[{index}] SHA-256 mismatch: {relative}")
        normalized.append(
            {
                "path": relative,
                "sha256": actual,
                "claim": _require_text(item.get("claim"), f"{field}[{index}].claim"),
            }
        )
    return normalized


def _validate_record_shape(record: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    if record.get("schema_version") != 1:
        raise ExperienceLedgerError("experience schema_version must be 1")
    experience_id = _require_text(record.get("experience_id"), "experience_id")
    if EXPERIENCE_ID.fullmatch(experience_id) is None:
        raise ExperienceLedgerError("experience_id must match EXP-[A-Z0-9-]")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ExperienceLedgerError("revision must be a positive integer")
    _require_timezone_timestamp(record.get("recorded_at"), "recorded_at")
    _require_timezone_timestamp(record.get("as_of"), "as_of")
    writer = _require_text(record.get("created_by_task"), "created_by_task")
    if writer not in WRITER_TASKS:
        raise ExperienceLedgerError(f"unsupported experience writer task: {writer}")

    observation = record.get("observation")
    if not isinstance(observation, dict):
        raise ExperienceLedgerError("observation must be an object")
    _require_text(observation.get("statement"), "observation.statement")

    hypothesis = record.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise ExperienceLedgerError("hypothesis must be an object")
    _require_text(hypothesis.get("statement"), "hypothesis.statement")
    _require_text(
        hypothesis.get("falsification_criteria"),
        "hypothesis.falsification_criteria",
    )

    _validate_evidence_items(
        record.get("evidence"),
        field="evidence",
        repo_root=repo_root,
        allow_empty=False,
    )
    _validate_evidence_items(
        record.get("counterevidence"),
        field="counterevidence",
        repo_root=repo_root,
        allow_empty=True,
    )
    counter_search = record.get("counterevidence_search")
    if not isinstance(counter_search, dict):
        raise ExperienceLedgerError("counterevidence_search must be an object")
    counter_status = _require_text(
        counter_search.get("status"), "counterevidence_search.status"
    )
    if counter_status not in COUNTEREVIDENCE_SEARCH_STATUSES:
        raise ExperienceLedgerError("invalid counterevidence_search.status")
    _require_text(counter_search.get("notes"), "counterevidence_search.notes")
    if counter_status == "FOUND" and not record["counterevidence"]:
        raise ExperienceLedgerError("counterevidence_search FOUND requires counterevidence")

    scope = record.get("scope")
    if not isinstance(scope, dict):
        raise ExperienceLedgerError("scope must be an object")
    if scope.get("market_scope") != "CN_MAINLAND_PUBLIC_MARKETS":
        raise ExperienceLedgerError("scope.market_scope must be CN_MAINLAND_PUBLIC_MARKETS")
    asset_types = scope.get("asset_types")
    if not isinstance(asset_types, list) or not asset_types or not all(
        isinstance(item, str) and item for item in asset_types
    ):
        raise ExperienceLedgerError("scope.asset_types must be a non-empty string list")
    _require_text(scope.get("horizon"), "scope.horizon")
    for field in ("regimes", "exclusions"):
        values = scope.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ExperienceLedgerError(f"scope.{field} must be a string list")

    validation = record.get("validation")
    if not isinstance(validation, dict):
        raise ExperienceLedgerError("validation must be an object")
    validation_status = _require_text(validation.get("status"), "validation.status")
    if validation_status not in VALIDATION_STATUSES:
        raise ExperienceLedgerError("invalid validation.status")
    _require_text(validation.get("method"), "validation.method")
    completed = validation.get("completed_round_trips")
    eligible = validation.get("eligible_round_trips")
    if (
        not isinstance(completed, int)
        or isinstance(completed, bool)
        or completed < 0
        or not isinstance(eligible, int)
        or isinstance(eligible, bool)
        or eligible < 0
        or eligible > completed
    ):
        raise ExperienceLedgerError("validation round-trip counts are invalid")
    if not isinstance(validation.get("metrics"), dict):
        raise ExperienceLedgerError("validation.metrics must be an object")
    limitations = validation.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        raise ExperienceLedgerError("validation.limitations must be a string list")

    impact = record.get("strategy_impact")
    if not isinstance(impact, dict):
        raise ExperienceLedgerError("strategy_impact must be an object")
    impact_status = _require_text(impact.get("status"), "strategy_impact.status")
    if impact_status not in STRATEGY_IMPACT_STATUSES:
        raise ExperienceLedgerError("experience records cannot self-declare a promoted rule")
    _require_text(impact.get("target"), "strategy_impact.target")
    _require_text(impact.get("proposed_change"), "strategy_impact.proposed_change")
    _require_text(impact.get("rollback_condition"), "strategy_impact.rollback_condition")
    if impact_status == "PROPOSED_ONLY":
        _require_text(impact.get("evolution_run_id"), "strategy_impact.evolution_run_id")
        candidate_sha = _require_text(
            impact.get("candidate_strategy_sha256"),
            "strategy_impact.candidate_strategy_sha256",
        )
        if re.fullmatch(r"[0-9a-f]{64}", candidate_sha) is None:
            raise ExperienceLedgerError("candidate_strategy_sha256 must be lowercase SHA-256")

    previous = record.get("previous_record_sha256")
    if previous is not None and (
        not isinstance(previous, str) or re.fullmatch(r"[0-9a-f]{64}", previous) is None
    ):
        raise ExperienceLedgerError("previous_record_sha256 must be null or lowercase SHA-256")
    record_sha = _require_text(record.get("record_sha256"), "record_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", record_sha) is None:
        raise ExperienceLedgerError("record_sha256 must be lowercase SHA-256")
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    if sha256_bytes(canonical_json_bytes(payload)) != record_sha:
        raise ExperienceLedgerError(f"record SHA-256 mismatch for {experience_id} revision {revision}")
    return record


def _record_paths(experience_root: Path) -> list[Path]:
    if not experience_root.exists():
        return []
    return sorted(path for path in experience_root.glob("*/experience.json") if path.is_file())


def _load_records(
    experience_root: Path,
    *,
    repo_root: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in _record_paths(experience_root):
        if path.is_symlink():
            raise ExperienceLedgerError(f"experience record must not be a symlink: {path}")
        records.append((path, _validate_record_shape(_read_json_object(path), repo_root=repo_root)))

    by_experience: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for item in records:
        by_experience.setdefault(item[1]["experience_id"], []).append(item)
    for experience_id, items in by_experience.items():
        items.sort(key=lambda item: item[1]["revision"])
        for index, (_, record) in enumerate(items, start=1):
            if record["revision"] != index:
                raise ExperienceLedgerError(f"non-contiguous revisions for {experience_id}")
            expected_previous = None if index == 1 else items[index - 2][1]["record_sha256"]
            if record.get("previous_record_sha256") != expected_previous:
                raise ExperienceLedgerError(f"revision chain mismatch for {experience_id}")
    return records


def validate_experience_record_file(
    path: Path,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if path.is_symlink():
        raise ExperienceLedgerError(f"experience record must not be a symlink: {path}")
    return _validate_record_shape(_read_json_object(path), repo_root=repo_root)


def _minimum_eligible_round_trips(mode_lock_path: Path) -> int:
    mode_lock = _read_json_object(mode_lock_path)
    policy = mode_lock.get("evolution_policy")
    if not isinstance(policy, dict):
        raise ExperienceLedgerError("MODE_LOCK evolution_policy is missing")
    minimum = policy.get("minimum_completed_virtual_trades_for_parameter_upgrade")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise ExperienceLedgerError("MODE_LOCK minimum completed trade count is invalid")
    if policy.get("walk_forward_and_independent_oos_required") is not True:
        raise ExperienceLedgerError("MODE_LOCK must require walk-forward and independent OOS")
    if policy.get("trusted_evaluator_required") is not True:
        raise ExperienceLedgerError("MODE_LOCK must require a trusted evaluator")
    experience_policy = mode_lock.get("experience_ledger_policy")
    if not isinstance(experience_policy, dict):
        raise ExperienceLedgerError("MODE_LOCK experience_ledger_policy is missing")
    required_policy = {
        "preopen_reader": "required_fail_closed",
        "unpromoted_strategy_effect": "forbid",
        "promotion_authority": "protected_static_verifier_only",
        "active_rule_requires_live_strategy_hash_match": True,
    }
    for field, expected in required_policy.items():
        if experience_policy.get(field) != expected:
            raise ExperienceLedgerError(f"MODE_LOCK experience policy drift: {field}")
    return minimum


def _promotion_state(
    path: Path,
    record: dict[str, Any],
    *,
    minimum: int,
    strategy_sha256: str,
    verifier_source_sha256: str,
    mode_lock_sha256: str,
) -> tuple[str, list[str]]:
    if record["validation"]["status"] == "INVALIDATED":
        return "INVALIDATED", ["EXPERIENCE_INVALIDATED"]
    if record["strategy_impact"]["status"] != "PROPOSED_ONLY":
        return "NON_BINDING", ["NO_STRATEGY_CHANGE_PROPOSED"]
    reasons: list[str] = []
    if record["validation"]["status"] != "SUPPORTED":
        reasons.append("VALIDATION_NOT_SUPPORTED")
    if record["validation"]["eligible_round_trips"] < minimum:
        reasons.append(
            f"ELIGIBLE_ROUND_TRIPS_{record['validation']['eligible_round_trips']}_LT_{minimum}"
        )
    gate_path = path.parent / "gate.json"
    if not gate_path.exists():
        reasons.append("INDEPENDENT_GATE_MISSING")
        return "PROPOSED_ONLY", reasons
    gate = _read_json_object(gate_path)
    if gate.get("decision") != "ACCEPTED":
        reasons.append(f"GATE_DECISION_{gate.get('decision', 'UNKNOWN')}")
    if gate.get("verifier", {}).get("source_sha256") != verifier_source_sha256:
        reasons.append("UNTRUSTED_GATE_VERIFIER")
    if gate.get("source_sha256", {}).get("mode_lock") != mode_lock_sha256:
        reasons.append("GATE_MODE_LOCK_HASH_MISMATCH")
    if gate.get("run_id") != record["strategy_impact"].get("evolution_run_id"):
        reasons.append("EVOLUTION_RUN_ID_MISMATCH")
    gate_ledger = gate.get("ledger", {})
    if int(gate_ledger.get("eligible_sample_count", -1)) < minimum:
        reasons.append("GATE_ELIGIBLE_SAMPLE_COUNT_INSUFFICIENT")
    claim_boundary = gate.get("claim_boundary", {})
    for name in ("walk_forward", "independent_oos", "rollback_replay"):
        if claim_boundary.get(name) != "VERIFIED":
            reasons.append(f"{name.upper()}_NOT_VERIFIED")
    if claim_boundary.get("accepted_path") != "ENABLED_BY_TRUSTED_EVALUATOR":
        reasons.append("TRUSTED_ACCEPTED_PATH_NOT_ENABLED")
    experience_evidence = gate.get("evidence", {}).get("experience", {})
    if experience_evidence.get("sha256") != sha256_file(path):
        reasons.append("GATE_EXPERIENCE_HASH_MISMATCH")
    candidate = gate.get("candidate") or {}
    candidate_sha = record["strategy_impact"].get("candidate_strategy_sha256")
    if candidate.get("sha256") != candidate_sha:
        reasons.append("GATE_CANDIDATE_HASH_MISMATCH")
    if strategy_sha256 != candidate_sha:
        reasons.append("LIVE_STRATEGY_NOT_MATCHING_ACCEPTED_CANDIDATE")
    if reasons:
        return "PROPOSED_ONLY", sorted(set(reasons))
    return "PROMOTED_RULE", []


def validate_experience_ledger(
    experience_root: Path = DEFAULT_EXPERIENCE_ROOT,
    *,
    repo_root: Path = REPO_ROOT,
    mode_lock_path: Path = DEFAULT_MODE_LOCK,
    strategy_path: Path = DEFAULT_STRATEGY,
) -> dict[str, Any]:
    records = _load_records(experience_root, repo_root=repo_root)
    minimum = _minimum_eligible_round_trips(mode_lock_path)
    strategy_sha = sha256_file(strategy_path)
    verifier_source_sha = sha256_file(repo_root / "vibe_finance/evolution.py")
    mode_lock_sha = sha256_file(mode_lock_path)
    latest: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, record in records:
        current = latest.get(record["experience_id"])
        if current is None or record["revision"] > current[1]["revision"]:
            latest[record["experience_id"]] = (path, record)

    summaries: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []
    proposed_only = 0
    for experience_id in sorted(latest):
        path, record = latest[experience_id]
        promotion, reasons = _promotion_state(
            path,
            record,
            minimum=minimum,
            strategy_sha256=strategy_sha,
            verifier_source_sha256=verifier_source_sha,
            mode_lock_sha256=mode_lock_sha,
        )
        summary = {
            "experience_id": experience_id,
            "revision": record["revision"],
            "record_sha256": record["record_sha256"],
            "record_path": path.resolve().relative_to(repo_root.resolve()).as_posix()
            if repo_root.resolve() in path.resolve().parents
            else str(path),
            "as_of": record["as_of"],
            "observation": record["observation"]["statement"],
            "validation_status": record["validation"]["status"],
            "strategy_status": promotion,
            "strategy_reasons": reasons,
            "strategy_target": record["strategy_impact"]["target"],
        }
        summaries.append(summary)
        if promotion == "PROMOTED_RULE":
            promoted.append(summary)
        elif promotion == "PROPOSED_ONLY":
            proposed_only += 1

    manifest = [
        {
            "path": (
                path.resolve().relative_to(repo_root.resolve()).as_posix()
                if repo_root.resolve() in path.resolve().parents
                else str(path.resolve())
            ),
            "sha256": sha256_file(path),
        }
        for path, _ in sorted(records, key=lambda item: str(item[0]))
    ]
    return {
        "schema_version": 1,
        "status": "PASS",
        "claim_boundary": "PROMOTED_RULES_ONLY_AFTER_CLOSED_LOOP_AND_INDEPENDENT_GATE",
        "experience_root": str(experience_root),
        "record_count": len(records),
        "experience_count": len(latest),
        "promoted_rule_count": len(promoted),
        "proposed_only_count": proposed_only,
        "minimum_eligible_round_trips": minimum,
        "strategy_sha256": strategy_sha,
        "ledger_sha256": sha256_bytes(canonical_json_bytes(manifest)),
        "experiences": summaries,
        "active_strategy_rules": promoted,
    }


def write_experience_record(
    draft_path: Path,
    output_path: Path,
    *,
    experience_root: Path = DEFAULT_EXPERIENCE_ROOT,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    draft = _read_json_object(draft_path)
    root = experience_root.resolve()
    candidate = output_path.resolve()
    if candidate.name != "experience.json" or candidate.parent.parent != root:
        raise ExperienceLedgerError(
            "experience output must be reports/evolution/<run-id>/experience.json"
        )
    if candidate.exists():
        raise FileExistsError(f"experience record already exists: {candidate}")
    lock_name = sha256_bytes(str(root).encode("utf-8"))[:16]
    lock_path = Path(tempfile.gettempdir()) / f"vibe-finance-experience-{lock_name}.lock"
    with advisory_file_lock(lock_path, exclusive=True):
        existing = _load_records(experience_root, repo_root=repo_root)
        experience_id = _require_text(draft.get("experience_id"), "experience_id")
        revisions = [
            record
            for _, record in existing
            if record["experience_id"] == experience_id
        ]
        revisions.sort(key=lambda item: item["revision"])
        expected_revision = len(revisions) + 1
        if draft.get("revision") != expected_revision:
            raise ExperienceLedgerError(
                f"revision for {experience_id} must be {expected_revision}"
            )
        expected_previous = revisions[-1]["record_sha256"] if revisions else None
        if draft.get("previous_record_sha256") != expected_previous:
            raise ExperienceLedgerError(
                f"previous_record_sha256 does not match latest revision for {experience_id}"
            )
        record = dict(draft)
        if "record_sha256" in record:
            raise ExperienceLedgerError("draft must not supply record_sha256")
        record["record_sha256"] = sha256_bytes(canonical_json_bytes(record))
        _validate_record_shape(record, repo_root=repo_root)
        if output_path.parent.is_symlink():
            raise ExperienceLedgerError("experience run directory must not be a symlink")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(
            record,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        fsync_directory(candidate.parent)
    return {
        "status": "RECORDED",
        "experience_id": record["experience_id"],
        "revision": record["revision"],
        "record_sha256": record["record_sha256"],
        "path": str(output_path),
    }


def load_preopen_experience_context(
    experience_root: Path = DEFAULT_EXPERIENCE_ROOT,
    *,
    repo_root: Path = REPO_ROOT,
    mode_lock_path: Path = DEFAULT_MODE_LOCK,
    strategy_path: Path = DEFAULT_STRATEGY,
) -> dict[str, Any]:
    ledger = validate_experience_ledger(
        experience_root,
        repo_root=repo_root,
        mode_lock_path=mode_lock_path,
        strategy_path=strategy_path,
    )
    return {
        "status": ledger["status"],
        "claim_boundary": ledger["claim_boundary"],
        "ledger_sha256": ledger["ledger_sha256"],
        "record_count": ledger["record_count"],
        "experience_count": ledger["experience_count"],
        "promoted_rule_count": ledger["promoted_rule_count"],
        "proposed_only_count": ledger["proposed_only_count"],
        "active_strategy_rules": ledger["active_strategy_rules"],
        "review_items": ledger["experiences"],
    }
