from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .evolution import (
    REPO_ROOT,
    append_order_event,
    canonical_json_bytes,
    verify_event_ledger,
    verify_portfolio_projection,
)


class TransactionError(RuntimeError):
    """Raised when a prepared financial-state transaction cannot be recovered safely."""


FaultHook = Callable[[str], None]


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _publish_immutable(path: Path, payload: bytes) -> None:
    expected = _sha256_bytes(payload)
    if path.exists():
        if _sha256_file(path) != expected:
            raise TransactionError(f"immutable artifact conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(f"short artifact write: {written}/{len(payload)}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def state_lock_path(ledger_path: Path) -> Path:
    production = (REPO_ROOT / "data/ledger/portfolio.json").resolve()
    if ledger_path.resolve() == production:
        path = Path.home() / ".cache/vibe-finance/state.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return ledger_path.parent / ".vibe-finance-state.lock"


@contextmanager
def locked_state(ledger_path: Path, *, exclusive: bool) -> Iterator[None]:
    path = state_lock_path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def transaction_root(ledger_path: Path) -> Path:
    return ledger_path.parent / "transactions"


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TransactionError(f"cannot read transaction object {path}: {error}") from error
    if not isinstance(value, dict):
        raise TransactionError(f"transaction object must be a JSON object: {path}")
    return value


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def prepare_run_transaction(
    *,
    run_id: str,
    ledger_path: Path,
    orders_log: Path,
    recorded_at: str,
    portfolio: dict[str, Any],
    decision_path: Path,
    decision: dict[str, Any],
    report_path: Path,
    report_text: str,
    heartbeat: dict[str, Any],
    events: list[dict[str, Any]],
    auxiliary_files: list[tuple[Path, bytes]] | None = None,
    fault_hook: FaultHook | None = None,
) -> dict[str, Any]:
    if not ledger_path.exists():
        raise TransactionError("portfolio must exist before transaction preparation")
    verified = verify_event_ledger(orders_log)
    root = transaction_root(ledger_path)
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    try:
        run_dir.mkdir()
    except FileExistsError as error:
        raise TransactionError(f"transaction run_id already exists: {run_id}") from error
    prepared_events = [
        {
            "event_id": hashlib.sha256(f"{run_id}:{index}".encode("utf-8")).hexdigest(),
            "payload": event,
        }
        for index, event in enumerate(events)
    ]
    prepared_auxiliary: list[dict[str, Any]] = []
    for path, payload in auxiliary_files or []:
        try:
            after_text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TransactionError("auxiliary transaction files must be UTF-8") from error
        prepared_auxiliary.append(
            {
                "path": str(path.resolve()),
                "base_exists": path.exists(),
                "base_sha256": _sha256_file(path) if path.exists() else None,
                "after_text": after_text,
                "after_sha256": _sha256_bytes(payload),
            }
        )
    prepare = {
        "schema_version": 1,
        "run_id": run_id,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "recorded_at": recorded_at,
        "ledger_path": str(ledger_path.resolve()),
        "orders_log": str(orders_log.resolve()),
        "base_portfolio_sha256": _sha256_file(ledger_path),
        "base_event_count": int(verified["event_count"]),
        "base_event_head_sha256": str(verified["head_event_sha256"]),
        "portfolio": portfolio,
        "decision_path": str(decision_path.resolve()),
        "decision": decision,
        "report_path": str(report_path.resolve()),
        "report_text": report_text,
        "heartbeat_path": str((ledger_path.parent / "heartbeat.json").resolve()),
        "heartbeat": heartbeat,
        "events": prepared_events,
        "auxiliary_files": prepared_auxiliary,
    }
    _durable_replace(run_dir / "prepare.json", _json_bytes(prepare))
    _fault(fault_hook, "after_prepare")
    return recover_run_transaction(run_dir, fault_hook=fault_hook)


def recover_run_transaction(
    run_dir: Path, *, fault_hook: FaultHook | None = None
) -> dict[str, Any]:
    prepare_path = run_dir / "prepare.json"
    commit_path = run_dir / "commit.json"
    prepare = _load_object(prepare_path)
    if commit_path.exists():
        return _load_object(commit_path)

    ledger_path = Path(str(prepare["ledger_path"]))
    orders_log = Path(str(prepare["orders_log"]))
    verified = verify_event_ledger(orders_log)
    base_count = int(prepare["base_event_count"])
    planned = prepare.get("events", [])
    if not isinstance(planned, list) or int(verified["event_count"]) < base_count:
        raise TransactionError("event ledger is shorter than the prepared base")
    suffix = verified["_events"][base_count:]
    if len(suffix) > len(planned):
        raise TransactionError("event ledger contains a foreign suffix")
    for index, existing in enumerate(suffix):
        ledger = existing.get("_ledger")
        if not isinstance(ledger, dict) or ledger.get("event_id") != planned[index].get(
            "event_id"
        ):
            raise TransactionError("event ledger suffix diverges from the prepared batch")
        existing_payload = dict(existing)
        existing_payload.pop("_ledger", None)
        if canonical_json_bytes(existing_payload) != canonical_json_bytes(
            planned[index].get("payload")
        ):
            raise TransactionError(
                "event ledger suffix payload differs from the prepared batch"
            )
    if not suffix and verified["head_event_sha256"] != prepare["base_event_head_sha256"]:
        raise TransactionError("event ledger base head changed after preparation")
    for item in planned[len(suffix) :]:
        append_order_event(
            orders_log,
            item["payload"],
            event_id=str(item["event_id"]),
            recorded_at=str(prepare["recorded_at"]),
        )
    _fault(fault_hook, "after_events")

    portfolio_payload = _json_bytes(prepare["portfolio"])
    portfolio_after_sha = _sha256_bytes(portfolio_payload)
    current_portfolio_sha = _sha256_file(ledger_path)
    if current_portfolio_sha == prepare["base_portfolio_sha256"]:
        _durable_replace(ledger_path, portfolio_payload)
    elif current_portfolio_sha != portfolio_after_sha:
        raise TransactionError("portfolio differs from both prepared before and after images")
    _fault(fault_hook, "after_portfolio")

    decision_path = Path(str(prepare["decision_path"]))
    decision_payload = _json_bytes(prepare["decision"])
    _publish_immutable(decision_path, decision_payload)
    _fault(fault_hook, "after_decision")
    report_path = Path(str(prepare["report_path"]))
    report_payload = str(prepare["report_text"]).encode("utf-8")
    _publish_immutable(report_path, report_payload)
    _fault(fault_hook, "after_report")

    committed_auxiliary: list[dict[str, Any]] = []
    for item in prepare.get("auxiliary_files", []):
        path = Path(str(item["path"]))
        payload = str(item["after_text"]).encode("utf-8")
        after_sha = str(item["after_sha256"])
        if _sha256_bytes(payload) != after_sha:
            raise TransactionError("auxiliary after-image SHA-256 mismatch")
        if path.exists() and _sha256_file(path) == after_sha:
            pass
        elif bool(item.get("base_exists")):
            if not path.exists() or _sha256_file(path) != item.get("base_sha256"):
                raise TransactionError(f"auxiliary file changed after preparation: {path}")
            _durable_replace(path, payload)
        else:
            if path.exists():
                raise TransactionError(f"new auxiliary file appeared after preparation: {path}")
            _durable_replace(path, payload)
        committed_auxiliary.append({"path": str(path), "sha256": _sha256_file(path)})
    _fault(fault_hook, "after_auxiliary")

    post_events = verify_event_ledger(orders_log)
    projection = verify_portfolio_projection(post_events["_events"], ledger_path)
    heartbeat_path = Path(str(prepare["heartbeat_path"]))
    heartbeat_payload = _json_bytes(prepare["heartbeat"])
    _durable_replace(heartbeat_path, heartbeat_payload)
    _fault(fault_hook, "after_heartbeat")

    commit = {
        "schema_version": 1,
        "run_id": prepare["run_id"],
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "prepare_sha256": _sha256_file(prepare_path),
        "event_count": int(post_events["event_count"]),
        "event_head_sha256": str(post_events["head_event_sha256"]),
        "portfolio_sha256": _sha256_file(ledger_path),
        "decision_path": str(decision_path),
        "decision_sha256": _sha256_file(decision_path),
        "report_path": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "heartbeat_path": str(heartbeat_path),
        "heartbeat_sha256": _sha256_file(heartbeat_path),
        "projection_status": projection["status"],
        "auxiliary_files": committed_auxiliary,
    }
    _publish_immutable(commit_path, _json_bytes(commit))
    _fault(fault_hook, "after_commit")
    return commit


def recover_incomplete_transactions(
    ledger_path: Path, *, fault_hook: FaultHook | None = None
) -> list[dict[str, Any]]:
    root = transaction_root(ledger_path)
    if not root.exists():
        return []
    recovered: list[dict[str, Any]] = []
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        prepare = run_dir / "prepare.json"
        commit = run_dir / "commit.json"
        if prepare.exists() and not commit.exists():
            recovered.append(recover_run_transaction(run_dir, fault_hook=fault_hook))
        elif not prepare.exists():
            raise TransactionError(f"orphan transaction staging directory: {run_dir}")
    return recovered


def inspect_transaction_state(ledger_path: Path) -> dict[str, Any]:
    root = transaction_root(ledger_path)
    if not root.exists():
        return {"status": "LEGACY_NO_TRANSACTION_JOURNAL"}
    committed: list[tuple[str, Path, dict[str, Any]]] = []
    incomplete: list[str] = []
    orphaned: list[str] = []
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        prepare_path = run_dir / "prepare.json"
        commit_path = run_dir / "commit.json"
        if not prepare_path.exists():
            orphaned.append(run_dir.name)
            continue
        if not commit_path.exists():
            incomplete.append(run_dir.name)
            continue
        commit = _load_object(commit_path)
        committed.append((str(commit.get("committed_at", "")), run_dir, commit))
    if orphaned:
        return {
            "status": "INCOMPLETE",
            "reason": "ORPHAN_TRANSACTION_DIRECTORY",
            "incomplete_run_ids": orphaned,
            "recoverable": False,
        }
    if incomplete:
        return {
            "status": "INCOMPLETE",
            "reason": "PREPARED_NOT_COMMITTED",
            "incomplete_run_ids": incomplete,
            "recoverable": True,
        }
    if not committed:
        return {"status": "LEGACY_NO_TRANSACTION_JOURNAL"}
    _, _, latest = max(committed, key=lambda item: item[0])
    checks = (
        (ledger_path, latest.get("portfolio_sha256")),
        (Path(str(latest.get("decision_path"))), latest.get("decision_sha256")),
        (Path(str(latest.get("report_path"))), latest.get("report_sha256")),
        (Path(str(latest.get("heartbeat_path"))), latest.get("heartbeat_sha256")),
    )
    for path, expected in checks:
        if not path.exists() or _sha256_file(path) != expected:
            return {
                "status": "INCOMPLETE",
                "reason": f"COMMITTED_ARTIFACT_MISMATCH:{path}",
                "incomplete_run_ids": [str(latest.get("run_id"))],
                "recoverable": False,
            }
    for item in latest.get("auxiliary_files", []):
        path = Path(str(item.get("path")))
        if not path.exists() or _sha256_file(path) != item.get("sha256"):
            return {
                "status": "INCOMPLETE",
                "reason": f"COMMITTED_AUXILIARY_MISMATCH:{path}",
                "incomplete_run_ids": [str(latest.get("run_id"))],
                "recoverable": False,
            }
    return {"status": "COMMITTED", "latest_commit": latest}
