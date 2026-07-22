from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


class FrozenBaselineError(ValueError):
    """The immutable B0 source contract cannot be proven from repository history."""


_HARNESS = r'''
import hashlib
import json
import sys
import uuid
import socket
from datetime import datetime
from pathlib import Path

from vibe_finance import pipeline


class FixedDateTime(datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if value is None:
            raise RuntimeError("fixed evaluation clock was not initialized")
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


seed = sys.argv[1]
input_paths = [Path(value) for value in json.loads(sys.argv[2])]
output_root = Path(sys.argv[3])
strategy_path = Path(sys.argv[4])
counter = 0


def deny_network(*args, **kwargs):
    raise RuntimeError("network access is disabled inside the frozen B0 harness")


def deterministic_uuid4():
    global counter
    counter += 1
    digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).hexdigest()[:32]
    return uuid.UUID(hex=digest)


pipeline.datetime = FixedDateTime
pipeline.uuid.uuid4 = deterministic_uuid4
socket.create_connection = deny_network
socket.socket.connect = deny_network
first = json.loads(input_paths[0].read_text(encoding="utf-8"))
FixedDateTime.current = datetime.fromisoformat(first["as_of"].replace("Z", "+00:00"))
pipeline.initialize_ledger(output_root / "portfolio.json")
runs = []
for input_path in input_paths:
    snapshot = json.loads(input_path.read_text(encoding="utf-8"))
    FixedDateTime.current = datetime.fromisoformat(snapshot["as_of"].replace("Z", "+00:00"))
    runs.append(
        pipeline.run_pipeline(
            input_path=input_path,
            ledger_path=output_root / "portfolio.json",
            strategy_path=strategy_path,
            report_dir=output_root / "reports",
            orders_log=output_root / "orders.jsonl",
            mode="short",
        )
    )
status = pipeline.project_status(
    ledger_path=output_root / "portfolio.json",
    report_dir=output_root / "reports",
    max_age_hours=36.0,
    now=FixedDateTime.current,
)
print(json.dumps({"runs": runs, "status": status}, ensure_ascii=False, sort_keys=True, allow_nan=False))
'''


_DECISION_HARNESS = r'''
import hashlib
import json
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

from vibe_finance import pipeline


class FixedDateTime(datetime):
    current = None

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if value is None:
            raise RuntimeError("fixed evaluation clock was not initialized")
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


def deny_network(*args, **kwargs):
    raise RuntimeError("network access is disabled inside the frozen B0 decision harness")


seed, snapshot_name, state_name, output_name, strategy_name = sys.argv[1:6]
counter = 0


def deterministic_uuid4():
    global counter
    counter += 1
    digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).hexdigest()[:32]
    return uuid.UUID(hex=digest)


pipeline.datetime = FixedDateTime
pipeline.uuid.uuid4 = deterministic_uuid4
socket.create_connection = deny_network
socket.socket.connect = deny_network
snapshot = json.loads(Path(snapshot_name).read_text(encoding="utf-8"))
state = json.loads(Path(state_name).read_text(encoding="utf-8"))
strategy = json.loads(Path(strategy_name).read_text(encoding="utf-8"))
FixedDateTime.current = datetime.fromisoformat(snapshot["as_of"].replace("Z", "+00:00"))
ledger_path = Path(output_name) / "portfolio.json"
pipeline.initialize_ledger(ledger_path)
ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
ledger["cash_cny"] = state["cash_cny"]
ledger["positions"] = state["positions"]
ledger["pending_orders"] = []
warnings = pipeline.validate_snapshot(snapshot, strategy, now=FixedDateTime.current)
recommendations, blocks = pipeline._recommendations(ledger, snapshot, strategy, warnings)
orders = pipeline._create_orders(ledger, snapshot, strategy, recommendations, blocks)
print(
    json.dumps(
        {"recommendations": recommendations, "blocks": blocks, "orders": orders},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
)
'''


FROZEN_B0_EVALUATION_SYMBOLS = ("510300", "512100", "510880", "518880")


def _git(repo_root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FrozenBaselineError(f"git verification failed: {error}") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FrozenBaselineError(f"git {' '.join(arguments)} failed: {message}")
    return completed.stdout


def verify_frozen_b0_sources(repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    """Verify B0 historical sources without claiming an executable adapter exists."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        spec = manifest["candidates"]["B0"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise FrozenBaselineError(f"B0 manifest invalid: {error}") from error
    code_ref = str(spec.get("code_ref", ""))
    if len(code_ref) != 40 or any(character not in "0123456789abcdef" for character in code_ref):
        raise FrozenBaselineError("B0 code_ref must be a full lowercase Git commit")
    commit_type = _git(repo_root, "cat-file", "-t", code_ref).decode().strip()
    if commit_type != "commit":
        raise FrozenBaselineError("B0 code_ref is not a commit")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", code_ref, "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if ancestor.returncode != 0:
        raise FrozenBaselineError("B0 code_ref is not an ancestor of HEAD")
    strategy_bytes = _git(repo_root, "show", f"{code_ref}:config/strategy.json")
    pipeline_bytes = _git(repo_root, "show", f"{code_ref}:vibe_finance/pipeline.py")
    universe_bytes = _git(repo_root, "show", f"{code_ref}:config/universe.json")
    sources_bytes = _git(repo_root, "show", f"{code_ref}:config/sources.json")
    strategy_sha256 = hashlib.sha256(strategy_bytes).hexdigest()
    pipeline_sha256 = hashlib.sha256(pipeline_bytes).hexdigest()
    universe_sha256 = hashlib.sha256(universe_bytes).hexdigest()
    sources_sha256 = hashlib.sha256(sources_bytes).hexdigest()
    if strategy_sha256 != spec.get("strategy_sha256"):
        raise FrozenBaselineError("B0 strategy source hash mismatch")
    if pipeline_sha256 != spec.get("pipeline_sha256"):
        raise FrozenBaselineError("B0 pipeline source hash mismatch")
    if universe_sha256 != spec.get("universe_sha256"):
        raise FrozenBaselineError("B0 universe source hash mismatch")
    if sources_sha256 != spec.get("sources_sha256"):
        raise FrozenBaselineError("B0 sources registry hash mismatch")
    try:
        strategy = json.loads(strategy_bytes)
    except json.JSONDecodeError as error:
        raise FrozenBaselineError("B0 strategy source is not valid JSON") from error
    if strategy.get("version") != spec.get("strategy_version"):
        raise FrozenBaselineError("B0 strategy version mismatch")
    source_identity = {
        "code_ref": code_ref,
        "strategy_version": strategy["version"],
        "strategy_sha256": strategy_sha256,
        "pipeline_sha256": pipeline_sha256,
        "universe_sha256": universe_sha256,
        "sources_sha256": sources_sha256,
    }
    return {
        "schema_version": 1,
        "status": "FROZEN_SOURCES_VERIFIED",
        **source_identity,
        "source_identity_sha256": _canonical_sha256(source_identity),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "claim_boundary": {
            "source_identity_verified": True,
            "golden_behavior_verified": False,
            "strategy_returns_computed": False,
            "promotion_authorized": False,
        },
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _extract_git_archive(repo_root: Path, code_ref: str, target: Path) -> None:
    archive = _git(
        repo_root,
        "archive",
        "--format=tar",
        code_ref,
        "--",
        "vibe_finance",
        "config",
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        root = target.resolve()
        for member in handle.getmembers():
            destination = (target / member.name).resolve()
            if root not in destination.parents and destination != root:
                raise FrozenBaselineError("frozen Git archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise FrozenBaselineError("frozen Git archive contains an unsupported link")
        handle.extractall(target)


def _make_tree_read_only(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        child.chmod(0o555 if child.is_dir() else 0o444)
    path.chmod(0o555)


def _make_tree_owner_writable(path: Path) -> None:
    if not path.exists():
        return
    path.chmod(0o755)
    for child in path.rglob("*"):
        child.chmod(0o755 if child.is_dir() else 0o644)


def _protected_financial_hashes(repo_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in (
        "data/ledger/portfolio.json",
        "data/ledger/orders.jsonl",
        "data/ledger/heartbeat.json",
    ):
        path = repo_root / relative
        result[relative] = _sha256_file(path) if path.is_file() else "MISSING"
    return result


def _sandbox_environment(scratch_root: Path) -> dict[str, str]:
    return {
        "HOME": str(scratch_root / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TEMP": str(scratch_root / "tmp"),
        "TMP": str(scratch_root / "tmp"),
        "TMPDIR": str(scratch_root / "tmp"),
        "TZ": "UTC",
    }


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FrozenBaselineError(f"frozen B0 artifact already exists: {path}")
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"
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


def run_frozen_b0_mechanical(
    repo_root: Path,
    manifest_path: Path,
    snapshot_paths: list[Path],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Run unmodified frozen B0 core functions in an isolated deterministic sandbox.

    This is a mechanical decision replay, not a return backtest. Snapshot bytes
    remain caller-supplied and are content-bound into the resulting artifact.
    """
    if output_path is not None and output_path.exists():
        raise FrozenBaselineError(f"frozen B0 artifact already exists: {output_path}")
    source_contract = verify_frozen_b0_sources(repo_root, manifest_path)
    if not snapshot_paths:
        raise FrozenBaselineError("at least one snapshot is required")
    inputs: list[dict[str, Any]] = []
    previous_run_date = ""
    for path in snapshot_paths:
        try:
            raw = path.read_bytes()
            snapshot = json.loads(raw)
        except (OSError, json.JSONDecodeError) as error:
            raise FrozenBaselineError(f"cannot read B0 snapshot {path}: {error}") from error
        if not isinstance(snapshot, dict):
            raise FrozenBaselineError(f"B0 snapshot must be an object: {path}")
        run_date = snapshot.get("run_date")
        as_of = snapshot.get("as_of")
        if not isinstance(run_date, str) or len(run_date) != 10:
            raise FrozenBaselineError(f"B0 snapshot run_date invalid: {path}")
        if run_date <= previous_run_date:
            raise FrozenBaselineError("B0 snapshots must have unique increasing run_date values")
        if not isinstance(as_of, str):
            raise FrozenBaselineError(f"B0 snapshot as_of invalid: {path}")
        previous_run_date = run_date
        inputs.append(
            {
                "path": str(path.resolve()),
                "sha256": _sha256_bytes(raw),
                "run_date": run_date,
                "as_of": as_of,
                "bytes": raw,
            }
        )
    seed = _canonical_sha256(
        {
            "code_ref": source_contract["code_ref"],
            "input_sha256": [item["sha256"] for item in inputs],
        }
    )
    protected_before = _protected_financial_hashes(repo_root)
    with tempfile.TemporaryDirectory(prefix="vibe-frozen-b0-", dir="/tmp") as directory:
        sandbox = Path(directory)
        source_root = sandbox / "source"
        sandbox_inputs = sandbox / "inputs"
        scratch_root = sandbox / "scratch"
        source_root.mkdir()
        sandbox_inputs.mkdir()
        scratch_root.mkdir()
        (scratch_root / "home").mkdir()
        (scratch_root / "tmp").mkdir()
        _extract_git_archive(repo_root, source_contract["code_ref"], source_root)
        relative_inputs: list[str] = []
        for index, item in enumerate(inputs):
            target = sandbox_inputs / f"{index:05d}.json"
            target.write_bytes(item.pop("bytes"))
            target.chmod(0o444)
            relative_inputs.append(f"../inputs/{target.name}")
        sandbox_inputs.chmod(0o555)
        _make_tree_read_only(source_root)
        environment = _sandbox_environment(scratch_root)
        stable_output_root = "../scratch/out"
        stable_strategy_path = "config/strategy.json"
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _HARNESS,
                    seed,
                    json.dumps(relative_inputs),
                    stable_output_root,
                    stable_strategy_path,
                ],
                cwd=source_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
        finally:
            _make_tree_owner_writable(source_root)
            _make_tree_owner_writable(sandbox_inputs)
        if completed.returncode != 0:
            raise FrozenBaselineError(
                "frozen B0 harness failed: "
                + completed.stderr[-4000:]
                + completed.stdout[-1000:]
            )
        try:
            harness_result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise FrozenBaselineError("frozen B0 harness returned invalid JSON") from error
        output_root = scratch_root / "out"
        decisions: list[dict[str, Any]] = []
        for run in harness_result["runs"]:
            decision_path = source_root / str(run["decision"])
            decisions.append(json.loads(decision_path.read_text(encoding="utf-8")))
        ledger = json.loads((output_root / "portfolio.json").read_text(encoding="utf-8"))
        order_events = []
        orders_path = output_root / "orders.jsonl"
        if orders_path.exists():
            order_events = [
                json.loads(line)
                for line in orders_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        output_files = {
            str(path.relative_to(sandbox)): _sha256_file(path)
            for path in sorted(output_root.rglob("*"))
            if path.is_file()
        }
        behavior = {
            "runs": harness_result["runs"],
            "decisions": decisions,
            "final_ledger": ledger,
            "order_events": order_events,
            "status": harness_result["status"],
        }
    protected_after = _protected_financial_hashes(repo_root)
    if protected_after != protected_before:
        raise FrozenBaselineError(
            "production financial ledger changed during frozen B0 replay"
        )
    adapter_source_sha256 = _sha256_file(Path(__file__))
    result = {
        "schema_version": 1,
        "status": "PASS_FROZEN_B0_HISTORICAL_FIXTURE_SMOKE",
        "evidence_class": "FROZEN_B0_HISTORICAL_FIXTURE_SMOKE_NO_PERFORMANCE",
        "source_contract": source_contract,
        "adapter_source_sha256": adapter_source_sha256,
        "inputs": inputs,
        "determinism": {
            "clock": "EACH_SNAPSHOT_AS_OF",
            "uuid_seed_sha256": seed,
            "pythonhashseed": 0,
            "timezone": "UTC",
            "network_guard": "PYTHON_SOCKET_CONNECT_DENIED_APPLICATION_LEVEL",
            "environment_allowlist": sorted(environment),
            "source_tree_read_only": True,
            "input_files_read_only": True,
            "scratch_only_designated_writable_tree": True,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "behavior": behavior,
        "behavior_sha256": _canonical_sha256(behavior),
        "output_file_sha256": output_files,
        "protected_financial_files_unchanged": {
            key: value == protected_after[key]
            for key, value in protected_before.items()
        },
        "claim_boundary": {
            "historical_core_entered_execution_graph": True,
            "artifacts_saved_in_isolated_sandbox": True,
            "reload_replay_required": True,
            "fair_evaluation_adapter_ready": False,
            "historical_fixture_only": True,
            "strategy_returns_computed": False,
            "walk_forward_computed": False,
            "independent_oos_computed": False,
            "promotion_authorized": False,
            "financial_ledger_modified": False,
        },
    }
    result["replay_contract_sha256"] = _canonical_sha256(result)
    if output_path is not None:
        _write_immutable_json(output_path, result)
    return result


def run_frozen_b0_decision(
    repo_root: Path,
    manifest_path: Path,
    snapshot_path: Path,
    portfolio_state: dict[str, Any],
    *,
    trading_calendar_dates: list[str],
    data_bindings: dict[str, str],
) -> dict[str, Any]:
    """Execute only the frozen B0 recommendation/order functions for four ETFs."""
    source_contract = verify_frozen_b0_sources(repo_root, manifest_path)
    try:
        snapshot_bytes = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise FrozenBaselineError(f"cannot read B0 decision snapshot: {error}") from error
    if not isinstance(snapshot, dict):
        raise FrozenBaselineError("B0 decision snapshot must be an object")
    if (
        snapshot.get("is_trading_day") is not True
        or snapshot.get("market_state") != "closed"
        or snapshot.get("run_date") not in trading_calendar_dates
        or snapshot.get("daily_return_definition")
        != "adjacent_total_return_close_simple_return"
        or snapshot.get("history_definition")
        != "total_return_close_rescaled_to_signal_raw_close"
    ):
        raise FrozenBaselineError(
            "B0 decision requires a bound close with preregistered return semantics"
        )
    if trading_calendar_dates != sorted(set(trading_calendar_dates)):
        raise FrozenBaselineError("B0 trading calendar must be strictly increasing")
    calendar_sha256 = _canonical_sha256(trading_calendar_dates)
    if data_bindings.get("calendar_sha256") != calendar_sha256:
        raise FrozenBaselineError("B0 calendar binding mismatch")
    required_bindings = {
        "readiness_artifact_sha256",
        "canonical_panel_sha256",
        "source_registry_sha256",
        "calendar_sha256",
    }
    if set(data_bindings) != required_bindings or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in data_bindings.values()
    ):
        raise FrozenBaselineError("B0 data bindings invalid")
    assets = snapshot.get("assets")
    if not isinstance(assets, list) or tuple(
        str(asset.get("symbol", "")) for asset in assets if isinstance(asset, dict)
    ) != FROZEN_B0_EVALUATION_SYMBOLS:
        raise FrozenBaselineError("B0 assets must follow the frozen four-ETF universe order")
    for asset in assets:
        symbol = str(asset["symbol"])
        sources = asset.get("source_ids")
        history = asset.get("history")
        corporate_actions = asset.get("corporate_actions")
        if (
            not isinstance(sources, list)
            or len(set(sources)) < 2
            or asset.get("trading_status", "TRADING") != "TRADING"
            or not isinstance(history, list)
            or len(history) < 20
            or abs(float(history[-1]) - float(asset.get("close", 0))) > 1e-8
            or not isinstance(corporate_actions, list)
            or asset.get("history_adjusted_for_corporate_actions") is not True
        ):
            raise FrozenBaselineError(f"B0 point-in-time asset contract failed:{symbol}")
    indices = snapshot.get("indices")
    if not isinstance(indices, list) or not any(
        isinstance(item, dict)
        and item.get("broad") is True
        and isinstance(item.get("daily_return"), (int, float))
        for item in indices
    ):
        raise FrozenBaselineError("B0 broad-index daily return is required")
    if set(portfolio_state) != {"cash_cny", "positions"}:
        raise FrozenBaselineError("B0 portfolio state keys invalid")
    cash = portfolio_state.get("cash_cny")
    positions = portfolio_state.get("positions")
    if not isinstance(cash, (int, float)) or cash < 0 or not isinstance(positions, dict):
        raise FrozenBaselineError("B0 portfolio state invalid")
    if not set(positions).issubset(FROZEN_B0_EVALUATION_SYMBOLS):
        raise FrozenBaselineError("B0 portfolio contains an out-of-universe position")
    normalized_positions: dict[str, dict[str, Any]] = {}
    assets_by_symbol = {str(asset["symbol"]): asset for asset in assets}
    for symbol, position in sorted(positions.items()):
        if not isinstance(position, dict):
            raise FrozenBaselineError(f"B0 position invalid:{symbol}")
        quantity = position.get("quantity")
        average_cost = position.get("average_cost")
        if (
            not isinstance(quantity, int)
            or quantity <= 0
            or quantity % 100 != 0
            or not isinstance(average_cost, (int, float))
            or average_cost <= 0
        ):
            raise FrozenBaselineError(f"B0 position accounting invalid:{symbol}")
        asset = assets_by_symbol[symbol]
        normalized_positions[symbol] = {
            "quantity": quantity,
            "average_cost": float(average_cost),
            "name": str(asset.get("name", symbol)),
            "asset_type": str(asset.get("asset_type", "")),
            "risk_bucket": str(asset.get("risk_bucket", "")),
            "exposure_group": str(asset.get("exposure_group", symbol)),
            "acquired_date": str(position.get("acquired_date", "")),
            "last_buy_date": str(position.get("last_buy_date", "")),
        }
    normalized_state = {"cash_cny": float(cash), "positions": normalized_positions}
    seed = _canonical_sha256(
        {
            "source_identity_sha256": source_contract["source_identity_sha256"],
            "snapshot_sha256": _sha256_bytes(snapshot_bytes),
            "portfolio_state_sha256": _canonical_sha256(normalized_state),
        }
    )
    protected_before = _protected_financial_hashes(repo_root)
    with tempfile.TemporaryDirectory(prefix="vibe-frozen-b0-decision-", dir="/tmp") as directory:
        sandbox = Path(directory)
        source_root = sandbox / "source"
        input_root = sandbox / "inputs"
        scratch_root = sandbox / "scratch"
        source_root.mkdir()
        input_root.mkdir()
        scratch_root.mkdir()
        (scratch_root / "home").mkdir()
        (scratch_root / "tmp").mkdir()
        _extract_git_archive(repo_root, source_contract["code_ref"], source_root)
        snapshot_copy = input_root / "snapshot.json"
        state_copy = input_root / "state.json"
        snapshot_copy.write_bytes(snapshot_bytes)
        state_copy.write_text(
            json.dumps(normalized_state, ensure_ascii=False, sort_keys=True, allow_nan=False),
            encoding="utf-8",
        )
        snapshot_copy.chmod(0o444)
        state_copy.chmod(0o444)
        input_root.chmod(0o555)
        _make_tree_read_only(source_root)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _DECISION_HARNESS,
                    seed,
                    "../inputs/snapshot.json",
                    "../inputs/state.json",
                    "../scratch",
                    "config/strategy.json",
                ],
                cwd=source_root,
                env=_sandbox_environment(scratch_root),
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
        finally:
            _make_tree_owner_writable(source_root)
            _make_tree_owner_writable(input_root)
        if completed.returncode != 0:
            raise FrozenBaselineError(
                "frozen B0 decision harness failed: " + completed.stderr[-4000:]
            )
        try:
            raw_result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise FrozenBaselineError("frozen B0 decision harness returned invalid JSON") from error
    protected_after = _protected_financial_hashes(repo_root)
    if protected_after != protected_before:
        raise FrozenBaselineError("production financial ledger changed during B0 decision")
    projected_orders = [
        {
            "symbol": str(order["symbol"]),
            "side": str(order["side"]),
            "quantity": int(order["quantity"]),
            "signal_close": float(order["signal_close"]),
            "raw_open_limit": float(order["limit_price"]),
            "signal_type": str(order["signal_type"]),
            "score": float(order["signal_score"]),
        }
        for order in raw_result["orders"]
    ]
    signal = {
        "schema_version": 1,
        "status": "OK",
        "candidate": "B0",
        "signal_date": str(snapshot["run_date"]),
        "protocol_manifest_sha256": source_contract["manifest_sha256"],
        "frozen_source_contract_sha256": source_contract["source_identity_sha256"],
        "data_bindings": dict(sorted(data_bindings.items())),
        "frozen_snapshot_sha256": _sha256_bytes(snapshot_bytes),
        "portfolio_state_sha256": _canonical_sha256(normalized_state),
        "blocks": raw_result["blocks"],
        "recommendations": raw_result["recommendations"],
        "orders": projected_orders,
        "claim_boundary": {
            "frozen_decision_functions_executed": True,
            "historical_settlement_skipped": True,
            "historical_heartbeat_skipped": True,
            "next_open_observed": False,
            "strategy_returns_computed": False,
            "multi_day_state_adapter_verified": False,
            "promotion_authorized": False,
        },
    }
    signal["signal_sha256"] = _canonical_sha256(signal)
    return signal


def verify_frozen_b0_artifact(
    repo_root: Path, manifest_path: Path, artifact_path: Path
) -> dict[str, Any]:
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FrozenBaselineError(f"cannot read frozen B0 artifact: {error}") from error
    if not isinstance(artifact, dict):
        raise FrozenBaselineError("frozen B0 artifact must be a JSON object")
    claimed_contract = artifact.get("replay_contract_sha256")
    unsigned = {
        key: value for key, value in artifact.items() if key != "replay_contract_sha256"
    }
    mismatches: list[str] = []
    if _canonical_sha256(unsigned) != claimed_contract:
        mismatches.append("ARTIFACT_CONTRACT_HASH_MISMATCH")
    snapshot_paths = [Path(str(item.get("path", ""))) for item in artifact.get("inputs", [])]
    for item, path in zip(artifact.get("inputs", []), snapshot_paths):
        if not path.is_file():
            mismatches.append(f"INPUT_MISSING:{path}")
        elif _sha256_file(path) != item.get("sha256"):
            mismatches.append(f"INPUT_HASH_MISMATCH:{path}")
    replay: dict[str, Any] | None = None
    if not mismatches:
        try:
            replay = run_frozen_b0_mechanical(
                repo_root, manifest_path, snapshot_paths
            )
        except FrozenBaselineError as error:
            mismatches.append(f"REPLAY_FAILED:{error}")
        else:
            if replay["replay_contract_sha256"] != claimed_contract:
                mismatches.append("REPLAY_CONTRACT_MISMATCH")
    return {
        "schema_version": 1,
        "status": (
            "VERIFIED_FROZEN_B0_HISTORICAL_FIXTURE_SMOKE"
            if not mismatches
            else "INVALID"
        ),
        "artifact_path": str(artifact_path),
        "artifact_sha256": _sha256_file(artifact_path),
        "mismatches": mismatches,
        "replayed_behavior_sha256": replay.get("behavior_sha256") if replay else None,
        "claim_boundary": {
            "strategy_returns_verified": False,
            "promotion_authorized": False,
        },
    }
