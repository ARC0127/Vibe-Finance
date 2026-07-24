from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_TASK_CONTRACTS = Path("config/task_contracts.json")
DEFAULT_SYNC_SCRIPT = Path("scripts/sync_github.sh")

_ALLOWED_SIDE_EFFECT_CLASSES = {
    "read_only_report",
    "financial_state_transaction",
    "derived_document",
    "evidence_only_proposal",
    "governed_release",
}
_TASK_CASE_RE = re.compile(r"^\s{2}([a-z0-9][a-z0-9-]*)\)\s*$")
_ALLOWLIST_RE = re.compile(r"^\s*allowlist=\(([^)]*)\)\s*$")


class TaskContractError(ValueError):
    """Raised when task-contract governance is missing, invalid, or inconsistent."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskContractError(f"{field} must be a non-empty relative path")
    text = value.strip().rstrip("/")
    posix_path = PurePosixPath(text)
    if (
        text in {"", "."}
        or "\\" in text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:", text)
        or posix_path.as_posix() != text
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise TaskContractError(
            f"{field} must use a normalized repository-relative POSIX path: {value!r}"
        )
    return text


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise TaskContractError(f"{field} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise TaskContractError(f"{field} entries must be non-empty strings")
        normalized = item.strip()
        if normalized in result:
            raise TaskContractError(f"{field} contains duplicate entry: {normalized}")
        result.append(normalized)
    return result


def _path_list(value: object, *, field: str) -> list[str]:
    raw = _string_list(value, field=field)
    result = [_relative_path(item, field=field) for item in raw]
    if len(result) != len(set(result)):
        raise TaskContractError(f"{field} contains duplicate normalized paths")
    return result


def load_task_contracts(path: Path = DEFAULT_TASK_CONTRACTS) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskContractError(f"task-contract registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskContractError(f"invalid task-contract JSON: {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise TaskContractError("task-contract registry root must be an object")
    if payload.get("schema_version") != 1:
        raise TaskContractError("task-contract schema_version must be 1")
    if payload.get("simulation_only") is not True:
        raise TaskContractError("task-contract registry must be simulation_only=true")
    if payload.get("real_broker_integration") != "forbid":
        raise TaskContractError("real_broker_integration must be 'forbid'")
    if payload.get("caller_force_bypass") != "forbid":
        raise TaskContractError("caller_force_bypass must be 'forbid'")
    if payload.get("timezone") != "Asia/Shanghai":
        raise TaskContractError("task-contract timezone must be Asia/Shanghai")

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise TaskContractError("task-contract registry must contain tasks")

    normalized_tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise TaskContractError(f"tasks[{index}] must be an object")
        task_id = raw_task.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", task_id
        ):
            raise TaskContractError(f"tasks[{index}].task_id is invalid")
        if task_id in seen_ids:
            raise TaskContractError(f"duplicate task_id: {task_id}")
        seen_ids.add(task_id)

        purpose = raw_task.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise TaskContractError(f"{task_id}.purpose must be non-empty")
        side_effect_class = raw_task.get("side_effect_class")
        if side_effect_class not in _ALLOWED_SIDE_EFFECT_CLASSES:
            raise TaskContractError(
                f"{task_id}.side_effect_class is unsupported: {side_effect_class!r}"
            )
        if raw_task.get("allow_force_bypass") is not False:
            raise TaskContractError(f"{task_id}.allow_force_bypass must be false")

        runtime_write_roots = _path_list(
            raw_task.get("runtime_write_roots"),
            field=f"{task_id}.runtime_write_roots",
        )
        sync_allowlist = _path_list(
            raw_task.get("sync_allowlist"),
            field=f"{task_id}.sync_allowlist",
        )
        if not set(runtime_write_roots).issubset(sync_allowlist):
            missing = sorted(set(runtime_write_roots) - set(sync_allowlist))
            raise TaskContractError(
                f"{task_id}.runtime_write_roots are outside sync_allowlist: {missing}"
            )

        acceptance_checks = _string_list(
            raw_task.get("acceptance_checks"),
            field=f"{task_id}.acceptance_checks",
        )
        constraints = _string_list(
            raw_task.get("constraints"),
            field=f"{task_id}.constraints",
        )
        normalized_tasks.append(
            {
                "task_id": task_id,
                "purpose": purpose.strip(),
                "side_effect_class": side_effect_class,
                "runtime_write_roots": runtime_write_roots,
                "sync_allowlist": sync_allowlist,
                "acceptance_checks": acceptance_checks,
                "allow_force_bypass": False,
                "constraints": constraints,
            }
        )

    normalized = dict(payload)
    normalized["tasks"] = normalized_tasks
    return normalized


def parse_sync_allowlists(path: Path = DEFAULT_SYNC_SCRIPT) -> dict[str, list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise TaskContractError(f"sync script not found: {path}") from exc

    inside_task_case = False
    current_task: str | None = None
    allowlists: dict[str, list[str]] = {}
    for line in lines:
        if line.strip() == 'case "$task_id" in':
            inside_task_case = True
            continue
        if not inside_task_case:
            continue
        if line.strip() == "*)":
            break

        case_match = _TASK_CASE_RE.match(line)
        if case_match:
            current_task = case_match.group(1)
            continue
        allowlist_match = _ALLOWLIST_RE.match(line)
        if current_task and allowlist_match:
            try:
                values = shlex.split(allowlist_match.group(1), posix=True)
            except ValueError as exc:
                raise TaskContractError(
                    f"invalid sync allowlist for {current_task}: {exc}"
                ) from exc
            allowlists[current_task] = [
                _relative_path(value, field=f"sync.{current_task}") for value in values
            ]
            continue
        if line.strip() == ";;":
            current_task = None

    if not allowlists:
        raise TaskContractError(f"no task allowlists found in sync script: {path}")
    return allowlists


def audit_task_contracts(
    contracts_path: Path = DEFAULT_TASK_CONTRACTS,
    sync_script_path: Path = DEFAULT_SYNC_SCRIPT,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    registry = load_task_contracts(contracts_path)
    contract_by_id = {task["task_id"]: task for task in registry["tasks"]}
    script_allowlists = parse_sync_allowlists(sync_script_path)

    contract_ids = set(contract_by_id)
    script_ids = set(script_allowlists)
    if contract_ids != script_ids:
        raise TaskContractError(
            "task IDs differ between registry and sync script: "
            f"registry_only={sorted(contract_ids - script_ids)}, "
            f"script_only={sorted(script_ids - contract_ids)}"
        )

    for current_id, contract in contract_by_id.items():
        expected = contract["sync_allowlist"]
        actual = script_allowlists[current_id]
        if expected != actual:
            raise TaskContractError(
                f"sync allowlist mismatch for {current_id}: "
                f"registry={expected}, script={actual}"
            )

    if task_id is not None and task_id not in contract_by_id:
        raise TaskContractError(f"unknown task_id: {task_id}")

    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "claim_boundary": "STRUCTURAL_GOVERNANCE_AUDIT_ONLY",
        "simulation_only": True,
        "real_broker_integration": "forbid",
        "caller_force_bypass": "forbid",
        "timezone": registry["timezone"],
        "contracts_path": contracts_path.as_posix(),
        "contracts_sha256": _sha256(contracts_path),
        "sync_script_path": sync_script_path.as_posix(),
        "sync_script_sha256": _sha256(sync_script_path),
        "sync_allowlist_status": "PASS",
        "task_count": len(contract_by_id),
    }
    if task_id is None:
        result["tasks"] = [
            {
                "task_id": task["task_id"],
                "side_effect_class": task["side_effect_class"],
                "runtime_write_roots": task["runtime_write_roots"],
                "sync_allowlist": task["sync_allowlist"],
                "allow_force_bypass": False,
            }
            for task in registry["tasks"]
        ]
    else:
        result["task"] = contract_by_id[task_id]
    return result
