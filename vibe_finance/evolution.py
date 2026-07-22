from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "data/ledger/orders.jsonl"
DEFAULT_PORTFOLIO = REPO_ROOT / "data/ledger/portfolio.json"
DEFAULT_ANCHOR = REPO_ROOT / "config/ledger_legacy_anchor.json"
DEFAULT_MODE_LOCK = REPO_ROOT / "MODE_LOCK.json"
DEFAULT_STRATEGY = REPO_ROOT / "config/strategy.json"
LEGACY_DOMAIN = b"VIBE_FINANCE_LEGACY_PREFIX_V1\x00"
EVENT_DOMAIN = b"VIBE_FINANCE_ORDER_EVENT_V2\x00"
PIPELINE_EVENT_EVIDENCE_DOMAIN = b"VIBE_FINANCE_PIPELINE_EVENT_EVIDENCE_V1\x00"
GENESIS_EVENT_SHA256 = hashlib.sha256(b"VIBE_FINANCE_ORDER_LEDGER_V2_GENESIS\x00").hexdigest()
TRUSTED_PROVENANCE_EVALUATOR_AVAILABLE = False


class EvolutionGateError(ValueError):
    """Raised when evolution evidence is malformed or loses integrity."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise EvolutionGateError(f"value is not canonical JSON: {error}") from error
    return rendered.encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvolutionGateError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvolutionGateError(f"{path} must contain a JSON object")
    return value


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if any(not line.strip() for line in lines):
        raise EvolutionGateError("order ledger contains a blank line")
    events: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvolutionGateError(f"invalid order event at line {number}: {error}") from error
        if not isinstance(value, dict):
            raise EvolutionGateError(f"order event at line {number} must be an object")
        events.append(value)
    return events


def canonical_payload(events: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(event) + b"\n" for event in events)


def legacy_chain_head(events: list[dict[str, Any]]) -> str:
    return sha256_bytes(LEGACY_DOMAIN + canonical_payload(events))


def event_sha256(event: dict[str, Any]) -> str:
    payload = copy.deepcopy(event)
    ledger = payload.get("_ledger")
    if not isinstance(ledger, dict):
        raise EvolutionGateError("v2 event is missing _ledger object")
    ledger.pop("event_sha256", None)
    return sha256_bytes(EVENT_DOMAIN + canonical_json_bytes(payload))


def pipeline_event_core(event: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(event)
    payload.pop("_ledger", None)
    payload.pop("evolution_provenance", None)
    payload.pop("run_id", None)
    return payload


def pipeline_event_payload_sha256(event: dict[str, Any]) -> str:
    return sha256_bytes(
        PIPELINE_EVENT_EVIDENCE_DOMAIN + canonical_json_bytes(pipeline_event_core(event))
    )


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_repo_artifact(repo_root: Path, relative: Any) -> tuple[Path, str]:
    if not isinstance(relative, str):
        raise EvolutionGateError("pipeline evidence path must be a string")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvolutionGateError("pipeline evidence path must stay inside the repository")
    path = repo_root / candidate
    if path.is_symlink():
        raise EvolutionGateError("pipeline evidence path must not be a symlink")
    resolved_root = repo_root.resolve()
    resolved = path.resolve(strict=True)
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise EvolutionGateError("pipeline evidence path escapes the repository")
    return resolved, candidate.as_posix()


def _verify_git_bound_file(repo_root: Path, relative: Any, expected_sha: Any) -> Path:
    if not _valid_sha256(expected_sha):
        raise EvolutionGateError("pipeline evidence SHA-256 is invalid")
    path, git_relative = _safe_repo_artifact(repo_root, relative)
    if sha256_file(path) != expected_sha:
        raise EvolutionGateError("pipeline evidence working-tree SHA-256 mismatch")
    _require_git_metadata(repo_root)
    try:
        committed = subprocess.check_output(
            ["git", "-C", str(repo_root), "show", f"HEAD:{git_relative}"],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        raise EvolutionGateError("pipeline evidence is not committed in Git HEAD") from error
    if sha256_bytes(committed) != expected_sha:
        raise EvolutionGateError("pipeline evidence Git HEAD SHA-256 mismatch")
    return path


def verify_pipeline_event_provenance(event: dict[str, Any], repo_root: Path) -> bool:
    provenance = event.get("evolution_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema_version") != 2:
        return False
    try:
        if provenance.get("producer") != "vibe_finance.pipeline":
            raise EvolutionGateError("pipeline evidence producer is invalid")
        if provenance.get("event_payload_sha256") != pipeline_event_payload_sha256(event):
            raise EvolutionGateError("pipeline event payload SHA-256 mismatch")
        input_path = _verify_git_bound_file(
            repo_root, provenance.get("input_path"), provenance.get("input_sha256")
        )
        strategy_path = _verify_git_bound_file(
            repo_root,
            provenance.get("strategy_path"),
            provenance.get("strategy_sha256"),
        )
        decision_path = _verify_git_bound_file(
            repo_root,
            provenance.get("decision_manifest_path"),
            provenance.get("decision_manifest_sha256"),
        )
        decision = _read_json_object(decision_path)
        if decision.get("run_id") != event.get("run_id"):
            raise EvolutionGateError("pipeline event run_id differs from decision")
        if decision.get("mode") != provenance.get("pipeline_mode"):
            raise EvolutionGateError("pipeline event mode differs from decision")
        if decision.get("input_sha256") != provenance.get("input_sha256"):
            raise EvolutionGateError("pipeline input SHA-256 differs from decision")
        if decision.get("strategy_sha256") != provenance.get("strategy_sha256"):
            raise EvolutionGateError("pipeline strategy SHA-256 differs from decision")
        if sha256_file(input_path) != provenance.get("input_sha256"):
            raise EvolutionGateError("pipeline input SHA-256 changed during verification")
        if sha256_file(strategy_path) != provenance.get("strategy_sha256"):
            raise EvolutionGateError("pipeline strategy SHA-256 changed during verification")
        target = canonical_json_bytes(pipeline_event_core(event))
        matches = 0
        for key in ("fills", "events", "new_orders"):
            values = decision.get(key, [])
            if not isinstance(values, list):
                raise EvolutionGateError(f"decision {key} must be a list")
            matches += sum(
                canonical_json_bytes(value) == target
                for value in values
                if isinstance(value, dict)
            )
        if matches != 1:
            raise EvolutionGateError("pipeline event is not uniquely bound to its decision")
    except (EvolutionGateError, OSError, ValueError, TypeError):
        return False
    return True


def _git_output(repo_root: Path, *args: str) -> str:
    _require_git_metadata(repo_root)
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise EvolutionGateError(f"git {' '.join(args)} failed: {error.output.strip()}") from error


def _require_git_metadata(repo_root: Path) -> None:
    """Fail closed at the intended repository root instead of discovering a parent Git repo."""
    if not (repo_root / ".git").exists():
        raise EvolutionGateError(
            "GIT_METADATA_UNAVAILABLE: repository .git is absent; provenance writes are deferred"
        )


def verify_legacy_anchor(
    legacy_events: list[dict[str, Any]],
    ledger_path: Path = DEFAULT_LEDGER,
    anchor_path: Path = DEFAULT_ANCHOR,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    anchor = _read_json_object(anchor_path)
    required = {
        "source_commit",
        "source_blob_oid",
        "ledger_path",
        "legacy_line_count",
        "canonical_payload_sha256",
        "chain_head_sha256",
    }
    missing = sorted(required - anchor.keys())
    if missing:
        raise EvolutionGateError(f"legacy anchor is missing fields: {missing}")
    if anchor.get("schema_version") != 1:
        raise EvolutionGateError("legacy anchor schema_version must be 1")
    if anchor.get("policy") != "LEGACY_GIT_ANCHORED_READ_ONLY_PREFIX_V1":
        raise EvolutionGateError("legacy anchor policy is invalid")
    anchored_ledger = (repo_root / str(anchor["ledger_path"])).resolve()
    if ledger_path.resolve() != anchored_ledger:
        raise EvolutionGateError("verified ledger path does not match the legacy anchor")
    if int(anchor["legacy_line_count"]) != len(legacy_events):
        raise EvolutionGateError("legacy prefix line count does not match the anchor")

    payload = canonical_payload(legacy_events)
    payload_sha = sha256_bytes(payload)
    chain_head = sha256_bytes(LEGACY_DOMAIN + payload)
    if payload_sha != anchor["canonical_payload_sha256"]:
        raise EvolutionGateError("legacy prefix canonical payload hash mismatch")
    if chain_head != anchor["chain_head_sha256"]:
        raise EvolutionGateError("legacy prefix chain-head mismatch")

    commit = str(anchor["source_commit"])
    if len(commit) != 40:
        raise EvolutionGateError("legacy source_commit must be a full 40-character commit")
    _require_git_metadata(repo_root)
    ancestor = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise EvolutionGateError("legacy source_commit is not an ancestor of HEAD")

    ledger_spec = f"{commit}:{anchor['ledger_path']}"
    blob_oid = _git_output(repo_root, "rev-parse", ledger_spec)
    if blob_oid != anchor["source_blob_oid"]:
        raise EvolutionGateError("legacy source blob OID mismatch")
    try:
        git_bytes = subprocess.check_output(
            ["git", "-C", str(repo_root), "show", ledger_spec],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        raise EvolutionGateError(f"cannot read anchored ledger blob: {error.output!r}") from error
    try:
        git_lines = git_bytes.decode("utf-8").splitlines()
        git_events = [json.loads(line) for line in git_lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvolutionGateError(f"anchored ledger blob is not valid UTF-8 JSONL: {error}") from error
    if sha256_bytes(canonical_payload(git_events)) != payload_sha:
        raise EvolutionGateError("working legacy prefix differs from the anchored Git blob")

    return {
        "status": "VERIFIED_LEGACY_GIT_ANCHORED",
        "line_count": len(legacy_events),
        "canonical_payload_sha256": payload_sha,
        "chain_head_sha256": chain_head,
        "source_commit": commit,
        "source_blob_oid": blob_oid,
    }


def _verify_git_head_prefix(
    events: list[dict[str, Any]], ledger_path: Path, repo_root: Path
) -> dict[str, Any]:
    """Bind an operational ledger to the immutable prefix already stored in Git HEAD."""
    try:
        relative = ledger_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return {"status": "INTERNAL_CHAIN_ONLY", "committed_event_count": 0}
    if not (repo_root / ".git").exists():
        return {"status": "INTERNAL_CHAIN_ONLY", "committed_event_count": 0}

    try:
        head_bytes = subprocess.check_output(
            ["git", "-C", str(repo_root), "show", f"HEAD:{relative}"],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        raise EvolutionGateError(f"cannot read ledger prefix from Git HEAD: {error.output!r}") from error
    try:
        head_lines = head_bytes.decode("utf-8").splitlines()
        head_events = [json.loads(line) for line in head_lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvolutionGateError(f"Git HEAD ledger is not valid UTF-8 JSONL: {error}") from error
    if len(events) < len(head_events):
        raise EvolutionGateError("working ledger truncates the Git HEAD event prefix")
    if canonical_payload(events[: len(head_events)]) != canonical_payload(head_events):
        raise EvolutionGateError("working ledger rewrites the Git HEAD event prefix")
    return {
        "status": "VERIFIED_GIT_HEAD_PREFIX",
        "commit": _git_output(repo_root, "rev-parse", "HEAD"),
        "blob_oid": _git_output(repo_root, "rev-parse", f"HEAD:{relative}"),
        "ledger_path": relative,
        "committed_event_count": len(head_events),
        "canonical_payload_sha256": sha256_bytes(canonical_payload(head_events)),
    }


def verify_event_ledger(
    ledger_path: Path = DEFAULT_LEDGER,
    anchor_path: Path = DEFAULT_ANCHOR,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    events = _parse_jsonl(ledger_path)
    legacy_events: list[dict[str, Any]] = []
    v2_events: list[dict[str, Any]] = []
    seen_v2 = False
    for event in events:
        is_v2 = "_ledger" in event
        if is_v2:
            seen_v2 = True
            v2_events.append(event)
        elif seen_v2:
            raise EvolutionGateError("unchained legacy event appears after a v2 event")
        else:
            legacy_events.append(event)

    if legacy_events:
        anchor = verify_legacy_anchor(
            legacy_events,
            ledger_path=ledger_path,
            anchor_path=anchor_path,
            repo_root=repo_root,
        )
        previous_hash = anchor["chain_head_sha256"]
        expected_sequence = len(legacy_events) + 1
    else:
        anchor = {
            "status": "EMPTY_V2_GENESIS" if not events else "NO_LEGACY_PREFIX",
            "line_count": 0,
            "chain_head_sha256": GENESIS_EVENT_SHA256,
        }
        previous_hash = GENESIS_EVENT_SHA256
        expected_sequence = 1

    event_ids: set[str] = set()
    previous_recorded_at: datetime | None = None
    for offset, event in enumerate(v2_events):
        ledger = event.get("_ledger")
        if not isinstance(ledger, dict) or ledger.get("schema_version") != 2:
            raise EvolutionGateError("v2 event has an invalid _ledger schema")
        sequence = ledger.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise EvolutionGateError("v2 event sequence must be an integer")
        if sequence != expected_sequence + offset:
            raise EvolutionGateError("v2 event sequence is not contiguous")
        event_id = str(ledger.get("event_id", ""))
        if not event_id or event_id in event_ids:
            raise EvolutionGateError("v2 event_id is missing or duplicated")
        event_ids.add(event_id)
        if ledger.get("previous_event_sha256") != previous_hash:
            raise EvolutionGateError("v2 predecessor hash mismatch")
        calculated = event_sha256(event)
        if ledger.get("event_sha256") != calculated:
            raise EvolutionGateError("v2 event hash mismatch")
        try:
            recorded_at = datetime.fromisoformat(str(ledger["recorded_at"]))
        except (KeyError, ValueError) as error:
            raise EvolutionGateError("v2 recorded_at must be an ISO-8601 timestamp") from error
        if recorded_at.tzinfo is None:
            raise EvolutionGateError("v2 recorded_at must include a timezone")
        if previous_recorded_at is not None and recorded_at < previous_recorded_at:
            raise EvolutionGateError("v2 recorded_at values are not monotonic")
        previous_recorded_at = recorded_at
        previous_hash = calculated

    git_head_prefix = _verify_git_head_prefix(events, ledger_path, repo_root)
    status = (
        "PASS_COMMITTED_PREFIX_AND_CHAIN"
        if git_head_prefix["status"] == "VERIFIED_GIT_HEAD_PREFIX"
        else "PASS_INTERNAL_CHAIN_ONLY"
    )

    return {
        "status": status,
        "ledger_path": str(ledger_path),
        "event_count": len(events),
        "legacy_event_count": len(legacy_events),
        "v2_event_count": len(v2_events),
        "head_event_sha256": previous_hash,
        "next_sequence": len(events) + 1,
        "anchor": anchor,
        "git_head_prefix": git_head_prefix,
        "_events": events,
    }


def append_order_event(
    ledger_path: Path,
    event: dict[str, Any],
    *,
    event_id: str | None = None,
    recorded_at: str | None = None,
    anchor_path: Path = DEFAULT_ANCHOR,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if "_ledger" in event:
        raise EvolutionGateError("caller must not supply protected _ledger metadata")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if ledger_path.resolve() == DEFAULT_LEDGER.resolve():
        lock_path = Path.home() / ".cache/vibe-finance/order-ledger.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        verified = verify_event_ledger(ledger_path, anchor_path=anchor_path, repo_root=repo_root)
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id:
                raise EvolutionGateError("event_id must be a non-empty string")
            for existing in verified["_events"]:
                ledger = existing.get("_ledger")
                if not isinstance(ledger, dict) or ledger.get("event_id") != event_id:
                    continue
                existing_payload = copy.deepcopy(existing)
                existing_payload.pop("_ledger", None)
                if canonical_json_bytes(existing_payload) != canonical_json_bytes(event):
                    raise EvolutionGateError("event_id collision has different payload")
                return existing
        timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise EvolutionGateError("recorded_at must be an ISO-8601 timestamp") from error
        if parsed_timestamp.tzinfo is None:
            raise EvolutionGateError("recorded_at must include a timezone")

        value = copy.deepcopy(event)
        value["_ledger"] = {
            "schema_version": 2,
            "sequence": int(verified["next_sequence"]),
            "event_id": event_id or uuid.uuid4().hex,
            "recorded_at": timestamp,
            "previous_event_sha256": verified["head_event_sha256"],
        }
        value["_ledger"]["event_sha256"] = event_sha256(value)
        line = canonical_json_bytes(value) + b"\n"
        descriptor = os.open(ledger_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, line)
            if written != len(line):
                raise OSError(f"short append: {written}/{len(line)} bytes")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        post = verify_event_ledger(ledger_path, anchor_path=anchor_path, repo_root=repo_root)
        if post["head_event_sha256"] != value["_ledger"]["event_sha256"]:
            raise EvolutionGateError("post-append ledger head mismatch")
        return value


def derive_completed_round_trips(
    events: list[dict[str, Any]],
    *,
    repo_root: Path | None = None,
    committed_event_count: int | None = None,
) -> dict[str, Any]:
    positions: dict[str, Decimal] = {}
    open_episode: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    pending_orders: dict[str, tuple[dict[str, Any], int]] = {}
    terminal_order_ids: set[str] = set()
    filled_count = 0
    cash_delta = Decimal("0")
    cumulative_buy_notional = Decimal("0")
    cumulative_sell_notional = Decimal("0")
    cumulative_fees = Decimal("0")
    cumulative_realized_pnl = Decimal("0")
    cash_replay_complete = True
    position_costs: dict[str, Decimal] = {}

    def parse_time(value: Any, label: str, index: int) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise EvolutionGateError(f"{label} at event {index} must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise EvolutionGateError(f"{label} at event {index} must include a timezone")
        return parsed

    def parse_decimal(value: Any, label: str, index: int) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except Exception as error:
            raise EvolutionGateError(f"{label} at event {index} is invalid") from error
        if not parsed.is_finite():
            raise EvolutionGateError(f"{label} at event {index} is not finite")
        return parsed

    def money(value: Any, label: str, index: int) -> Decimal:
        return parse_decimal(value, label, index).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def has_sample_provenance(
        fill: dict[str, Any],
        pending: dict[str, Any],
        fill_index: int,
        pending_index: int,
    ) -> bool:
        if not TRUSTED_PROVENANCE_EVALUATOR_AVAILABLE:
            return False
        if repo_root is None or committed_event_count is None:
            return False
        if fill_index > committed_event_count or pending_index > committed_event_count:
            return False
        return verify_pipeline_event_provenance(
            pending, repo_root
        ) and verify_pipeline_event_provenance(fill, repo_root)

    for index, event in enumerate(events, 1):
        status = str(event.get("status", ""))
        order_id = str(event.get("order_id", ""))
        if status in {"PENDING_NEXT_OPEN", "PENDING_NEXT_NAV"}:
            if not order_id or order_id in pending_orders or order_id in terminal_order_ids:
                raise EvolutionGateError(f"pending order_id is missing or duplicated at event {index}")
            if event.get("simulation_only") is not True:
                raise EvolutionGateError(f"pending event {index} must be simulation_only")
            pending_orders[order_id] = (event, index)
            continue
        if status.startswith("CANCELLED"):
            if not order_id or order_id not in pending_orders or order_id in terminal_order_ids:
                raise EvolutionGateError(f"cancelled event {index} has no unique pending order")
            terminal_order_ids.add(order_id)
            pending_orders.pop(order_id)
            continue
        if status != "FILLED":
            continue
        if not order_id or order_id not in pending_orders or order_id in terminal_order_ids:
            raise EvolutionGateError(f"filled event {index} has no unique preceding pending order")
        pending, pending_index = pending_orders.pop(order_id)
        terminal_order_ids.add(order_id)
        if event.get("simulation_only") is not True:
            raise EvolutionGateError(f"filled event {index} must be simulation_only")
        symbol = str(event.get("symbol", ""))
        side = str(event.get("side", "")).upper()
        raw_quantity = event.get("confirmed_shares", event.get("quantity"))
        if not symbol or side not in {"BUY", "SELL"} or raw_quantity is None:
            raise EvolutionGateError(f"filled event {index} lacks symbol/side/quantity")
        quantity = parse_decimal(raw_quantity, "filled quantity", index)
        if not quantity.is_finite() or quantity <= 0:
            raise EvolutionGateError(f"filled event {index} has non-positive quantity")
        if symbol != str(pending.get("symbol", "")) or side != str(
            pending.get("side", "")
        ).upper():
            raise EvolutionGateError(f"filled event {index} differs from its pending order")
        pending_status = str(pending.get("status", ""))
        if pending_status == "PENDING_NEXT_NAV" and side == "BUY":
            pending_amount = money(pending.get("amount_cny"), "pending amount_cny", index)
            fill_amount = money(event.get("amount_cny"), "fill amount_cny", index)
            gross_amount = money(
                event.get("gross_amount_cny"), "fill gross_amount_cny", index
            )
            if pending_amount <= 0 or fill_amount != pending_amount or gross_amount != pending_amount:
                raise EvolutionGateError(f"filled event {index} differs from its NAV amount contract")
            try:
                precision = int(event.get("share_precision"))
            except (TypeError, ValueError) as error:
                raise EvolutionGateError(
                    f"filled event {index} has invalid share_precision"
                ) from error
            if precision < 0 or precision > 12 or event.get(
                "fund_share_rounding"
            ) != "ROUND_HALF_UP":
                raise EvolutionGateError(f"filled event {index} has unsupported share rounding")
            if pending.get("share_precision") != precision or pending.get(
                "fund_share_rounding"
            ) != "ROUND_HALF_UP":
                raise EvolutionGateError(f"filled event {index} changes its share rounding contract")
            nav = parse_decimal(event.get("fill_nav"), "fill_nav", index)
            rate = parse_decimal(
                event.get("purchase_fee_rate_applied"),
                "purchase_fee_rate_applied",
                index,
            )
            if nav <= 0 or rate < 0:
                raise EvolutionGateError(f"filled event {index} has invalid NAV or fee rate")
            net = pending_amount / (Decimal("1") + rate)
            expected_quantity = (net / nav).quantize(
                Decimal("1").scaleb(-precision), rounding=ROUND_HALF_UP
            )
            expected_fee = (pending_amount - net).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if quantity != expected_quantity or money(
                event.get("fund_fee_cny"), "fund_fee_cny", index
            ) != expected_fee:
                raise EvolutionGateError(f"filled event {index} violates its NAV share formula")
        else:
            pending_quantity = pending.get("confirmed_shares", pending.get("quantity"))
            if pending_quantity is None or parse_decimal(
                pending_quantity, "pending quantity", index
            ) != quantity:
                raise EvolutionGateError(f"filled event {index} differs from its pending order")
            if pending_status == "PENDING_NEXT_NAV":
                try:
                    precision = int(event.get("share_precision"))
                except (TypeError, ValueError) as error:
                    raise EvolutionGateError(
                        f"filled event {index} has invalid share_precision"
                    ) from error
                if event.get("fund_share_rounding") != "ROUND_HALF_UP":
                    raise EvolutionGateError(
                        f"filled event {index} has unsupported share rounding"
                    )
                nav = parse_decimal(event.get("fill_nav"), "fill_nav", index)
                rate = parse_decimal(
                    event.get("redemption_fee_rate_applied"),
                    "redemption_fee_rate_applied",
                    index,
                )
                expected_gross = (nav * quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                expected_fee = (expected_gross * rate).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if (
                    precision < 0
                    or money(event.get("gross_amount_cny"), "gross_amount_cny", index)
                    != expected_gross
                    or money(event.get("fund_fee_cny"), "fund_fee_cny", index)
                    != expected_fee
                ):
                    raise EvolutionGateError(
                        f"filled event {index} violates its NAV redemption formula"
                    )
        fill_time = parse_time(event.get("fill_as_of"), "fill_as_of", index)
        signal_time = parse_time(pending.get("signal_as_of"), "signal_as_of", index)
        if fill_time < signal_time:
            raise EvolutionGateError(f"filled event {index} predates its pending signal")
        provenance_eligible = has_sample_provenance(
            event, pending, index, pending_index
        )

        trade_notional: Decimal | None = None
        trade_fees: Decimal | None = None
        if pending_status == "PENDING_NEXT_NAV":
            if side == "BUY":
                trade_notional = money(event.get("amount_cny"), "amount_cny", index)
            else:
                trade_notional = money(
                    event.get("gross_amount_cny"), "gross_amount_cny", index
                )
            trade_fees = money(event.get("fund_fee_cny"), "fund_fee_cny", index)
        elif event.get("fill_price") is not None and event.get("total_fees_cny") is not None:
            trade_notional = money(
                parse_decimal(event.get("fill_price"), "fill_price", index) * quantity,
                "trade notional",
                index,
            )
            trade_fees = money(event.get("total_fees_cny"), "total_fees_cny", index)
        else:
            cash_replay_complete = False
        if trade_notional is not None and trade_fees is not None:
            realized = money(
                event.get("realized_pnl_cny", 0), "realized_pnl_cny", index
            )
            cumulative_fees += trade_fees
            cumulative_realized_pnl += realized
            if side == "BUY":
                cash_delta -= (
                    trade_notional
                    if pending_status == "PENDING_NEXT_NAV"
                    else trade_notional + trade_fees
                )
                cumulative_buy_notional += trade_notional
            else:
                cash_delta += trade_notional - trade_fees
                cumulative_sell_notional += trade_notional

        before = positions.get(symbol, Decimal("0"))
        if side == "BUY":
            if before == 0:
                open_episode[symbol] = {
                    "start_event": index,
                    "start_order_id": order_id,
                    "provenance_eligible": provenance_eligible,
                }
            else:
                open_episode[symbol]["provenance_eligible"] = (
                    open_episode[symbol]["provenance_eligible"] and provenance_eligible
                )
            after = before + quantity
            if trade_notional is not None and trade_fees is not None:
                acquisition_cost = (
                    trade_notional
                    if pending_status == "PENDING_NEXT_NAV"
                    else trade_notional + trade_fees
                )
                position_costs[symbol] = position_costs.get(
                    symbol, Decimal("0")
                ) + acquisition_cost
        else:
            if before <= 0 or quantity > before:
                raise EvolutionGateError(f"filled SELL at event {index} oversells {symbol}")
            after = before - quantity
            if symbol in position_costs and before > 0:
                average_cost = position_costs[symbol] / before
                position_costs[symbol] -= average_cost * quantity
                if after == 0:
                    position_costs.pop(symbol, None)
            if after == 0:
                episode = open_episode.pop(symbol, None)
                if episode is None:
                    raise EvolutionGateError(f"closed {symbol} without a tracked opening episode")
                completed.append(
                    {
                        "symbol": symbol,
                        **episode,
                        "end_event": index,
                        "end_order_id": order_id,
                        "provenance_eligible": (
                            episode["provenance_eligible"] and provenance_eligible
                        ),
                    }
                )
            elif symbol in open_episode:
                open_episode[symbol]["provenance_eligible"] = (
                    open_episode[symbol]["provenance_eligible"] and provenance_eligible
                )
        positions[symbol] = after
        filled_count += 1

    return {
        "filled_event_count": filled_count,
        "completed_round_trip_count": len(completed),
        "eligible_sample_count": sum(item["provenance_eligible"] for item in completed),
        "positions": {symbol: str(quantity) for symbol, quantity in sorted(positions.items()) if quantity != 0},
        "average_costs": {
            symbol: str(position_costs[symbol] / quantity)
            for symbol, quantity in sorted(positions.items())
            if quantity != 0 and symbol in position_costs
        },
        "pending_orders": [
            pending
            for pending, _ in sorted(
                pending_orders.values(), key=lambda item: str(item[0].get("order_id", ""))
            )
        ],
        "cash_replay_complete": cash_replay_complete,
        "cash_delta_cny": str(cash_delta),
        "cumulative_buy_notional_cny": str(cumulative_buy_notional),
        "cumulative_sell_notional_cny": str(cumulative_sell_notional),
        "cumulative_fees_cny": str(cumulative_fees),
        "cumulative_realized_pnl_cny": str(cumulative_realized_pnl),
        "open_episode_count": len(open_episode),
        "completed_round_trips": completed,
    }


def verify_portfolio_projection(
    events: list[dict[str, Any]],
    portfolio_path: Path,
    *,
    repo_root: Path | None = None,
    committed_event_count: int | None = None,
) -> dict[str, Any]:
    replay = derive_completed_round_trips(
        events,
        repo_root=repo_root,
        committed_event_count=committed_event_count,
    )
    portfolio = _read_json_object(portfolio_path)
    projected = {
        str(symbol): Decimal(str(value.get("quantity", 0)))
        for symbol, value in portfolio.get("positions", {}).items()
        if Decimal(str(value.get("quantity", 0))) != 0
    }
    replay_positions = {symbol: Decimal(quantity) for symbol, quantity in replay["positions"].items()}
    if replay_positions != projected:
        raise EvolutionGateError(
            f"portfolio positions differ from ledger replay: ledger={replay_positions}, portfolio={projected}"
        )
    portfolio_fills = int(portfolio.get("performance", {}).get("filled_trade_count", -1))
    if portfolio_fills != replay["filled_event_count"]:
        raise EvolutionGateError("portfolio filled_trade_count differs from ledger replay")
    performance = portfolio.get("performance", {})
    if "initial_project_capital_cny" in portfolio and replay["cash_replay_complete"]:
        infrastructure = portfolio.get("research_infrastructure", {})
        initial_investable = Decimal(str(portfolio["initial_project_capital_cny"])) - Decimal(
            str(infrastructure.get("reserved_cny", 0))
        )
        expected_cash = (initial_investable + Decimal(replay["cash_delta_cny"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        actual_cash = Decimal(str(portfolio.get("cash_cny"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if actual_cash != expected_cash:
            raise EvolutionGateError(
                f"portfolio cash differs from ledger replay: {actual_cash} != {expected_cash}"
            )
        aggregate_fields = {
            "cumulative_buy_notional_cny": replay["cumulative_buy_notional_cny"],
            "cumulative_sell_notional_cny": replay["cumulative_sell_notional_cny"],
            "cumulative_fees_cny": replay["cumulative_fees_cny"],
            "realized_pnl_cny": replay["cumulative_realized_pnl_cny"],
        }
        for field, expected in aggregate_fields.items():
            actual = Decimal(str(performance.get(field, 0))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if actual != Decimal(expected).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ):
                raise EvolutionGateError(f"portfolio {field} differs from ledger replay")
        for symbol, expected in replay["average_costs"].items():
            actual = Decimal(str(portfolio["positions"][symbol].get("average_cost")))
            if abs(actual - Decimal(expected)) > Decimal("0.000001"):
                raise EvolutionGateError(
                    f"portfolio average_cost differs from ledger replay for {symbol}"
                )

    if "pending_orders" in portfolio:
        def pending_projection(event: dict[str, Any]) -> tuple[str, ...]:
            quantity = event.get("quantity")
            amount = event.get("amount_cny")
            return (
                str(event.get("order_id", "")),
                str(event.get("status", "")),
                str(event.get("side", "")),
                str(event.get("symbol", "")),
                "" if quantity is None else str(Decimal(str(quantity))),
                "" if amount is None else str(Decimal(str(amount))),
            )

        expected_pending = sorted(
            pending_projection(event) for event in replay["pending_orders"]
        )
        actual_pending = sorted(
            pending_projection(event) for event in portfolio.get("pending_orders", [])
        )
        if actual_pending != expected_pending:
            raise EvolutionGateError("portfolio pending_orders differ from ledger replay")

    if "initial_project_capital_cny" in portfolio:
        market_values: list[Decimal] = []
        valuation_complete = True
        for position in portfolio.get("positions", {}).values():
            if position.get("market_value_cny") is not None:
                market_values.append(Decimal(str(position["market_value_cny"])))
            elif position.get("last_price") is not None:
                market_values.append(
                    Decimal(str(position["last_price"]))
                    * Decimal(str(position.get("quantity", 0)))
                )
            else:
                valuation_complete = False
                break
        if valuation_complete:
            investable = (
                Decimal(str(portfolio.get("cash_cny", 0))) + sum(market_values, Decimal("0"))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            reported_investable = Decimal(str(performance.get("investable_value_cny", 0))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if reported_investable != investable:
                raise EvolutionGateError("portfolio investable_value_cny is internally inconsistent")
            infrastructure = portfolio.get("research_infrastructure", {})
            remaining = Decimal(str(infrastructure.get("reserved_cny", 0))) - Decimal(
                str(infrastructure.get("spent_cny", 0))
            )
            project_equity = (investable + remaining).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            reported_equity = Decimal(str(performance.get("project_equity_cny", 0))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if reported_equity != project_equity:
                raise EvolutionGateError("portfolio project_equity_cny is internally inconsistent")
            initial = Decimal(str(portfolio["initial_project_capital_cny"]))
            reported_pnl = Decimal(str(performance.get("total_pnl_cny", 0))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if reported_pnl != project_equity - initial:
                raise EvolutionGateError("portfolio total_pnl_cny is internally inconsistent")
    return {"status": "PASS", **replay, "portfolio_filled_trade_count": portfolio_fills}


def _safe_proposal_path(proposal_dir: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvolutionGateError("proposal evidence path must be relative and cannot contain '..'")
    unresolved = proposal_dir / candidate
    if unresolved.is_symlink():
        raise EvolutionGateError("proposal evidence must not be a symlink")
    resolved_dir = proposal_dir.resolve()
    resolved = unresolved.resolve(strict=True)
    if resolved_dir != resolved and resolved_dir not in resolved.parents:
        raise EvolutionGateError("proposal evidence path escapes its run directory")
    return resolved


def _strategy_from_git(repo_root: Path, commit: str) -> tuple[str, str]:
    if len(commit) != 40:
        raise EvolutionGateError("baseline_ref must be a full 40-character commit")
    _require_git_metadata(repo_root)
    ancestor = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise EvolutionGateError("baseline_ref is not an ancestor of HEAD")
    try:
        payload = subprocess.check_output(
            ["git", "-C", str(repo_root), "show", f"{commit}:config/strategy.json"],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as error:
        raise EvolutionGateError(f"cannot load baseline strategy: {error.output!r}") from error
    return sha256_bytes(payload), _git_output(repo_root, "rev-parse", commit)


def verify_evolution_gate(
    proposal_path: Path,
    *,
    ledger_path: Path = DEFAULT_LEDGER,
    portfolio_path: Path = DEFAULT_PORTFOLIO,
    anchor_path: Path = DEFAULT_ANCHOR,
    mode_lock_path: Path = DEFAULT_MODE_LOCK,
    baseline_ref: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    if os.environ.get("VIBE_EVOLUTION_DECISION") or os.environ.get("VIBE_ROLLBACK_BASE"):
        raise EvolutionGateError("caller-supplied evolution decision or rollback base is forbidden")
    proposal = _read_json_object(proposal_path)
    if "decision" in proposal or "rollback_base" in proposal:
        raise EvolutionGateError("proposal must not contain decision or rollback_base")
    if proposal.get("schema_version") != 1 or not proposal.get("run_id"):
        raise EvolutionGateError("proposal schema_version=1 and run_id are required")

    ledger = verify_event_ledger(ledger_path, anchor_path=anchor_path, repo_root=repo_root)
    if ledger["status"] != "PASS_COMMITTED_PREFIX_AND_CHAIN":
        raise EvolutionGateError("evolution gate requires a ledger bound to the Git HEAD prefix")
    replay = verify_portfolio_projection(
        ledger["_events"],
        portfolio_path,
        repo_root=repo_root,
        committed_event_count=int(
            ledger["git_head_prefix"].get("committed_event_count", 0)
        ),
    )
    mode_lock = _read_json_object(mode_lock_path)
    policy = mode_lock.get("evolution_policy", {})
    minimum = int(policy.get("minimum_completed_virtual_trades_for_parameter_upgrade", 20))
    if policy.get("walk_forward_and_independent_oos_required") is not True:
        raise EvolutionGateError("MODE_LOCK must require walk-forward AND independent OOS")

    proposal_dir = proposal_path.parent
    reasons: list[str] = []
    candidate_info: dict[str, Any] | None = None
    candidate = proposal.get("candidate_strategy")
    if not isinstance(candidate, dict):
        reasons.append("CANDIDATE_STRATEGY_MISSING")
    else:
        if "path" not in candidate or "sha256" not in candidate:
            raise EvolutionGateError("candidate_strategy path and sha256 are required")
        candidate_path = _safe_proposal_path(proposal_dir, str(candidate["path"]))
        actual_sha = sha256_file(candidate_path)
        if actual_sha != candidate["sha256"]:
            raise EvolutionGateError("candidate strategy SHA-256 mismatch")
        candidate_value = _read_json_object(candidate_path)
        candidate_info = {
            "path": str(candidate_path),
            "sha256": actual_sha,
            "version": candidate_value.get("version", "UNKNOWN"),
        }

    if replay["eligible_sample_count"] < minimum:
        reasons.append(
            f"ELIGIBLE_ROUND_TRIPS_{replay['eligible_sample_count']}_LT_{minimum}"
        )
    evidence_reason = {
        "preregistration": "PREREGISTRATION_MISSING",
        "walk_forward": "WALK_FORWARD_ARTIFACT_MISSING",
        "out_of_sample": "INDEPENDENT_OOS_ARTIFACT_MISSING",
        "rollback_replay": "ROLLBACK_REPLAY_MISSING",
    }
    evidence: dict[str, Any] = {}
    for name, reason in evidence_reason.items():
        reference = proposal.get(name)
        if not isinstance(reference, dict):
            reasons.append(reason)
            evidence[name] = {"status": "MISSING"}
            continue
        if "path" not in reference or "sha256" not in reference:
            raise EvolutionGateError(f"{name} path and sha256 are required")
        path = _safe_proposal_path(proposal_dir, str(reference["path"]))
        actual = sha256_file(path)
        if actual != reference["sha256"]:
            raise EvolutionGateError(f"{name} SHA-256 mismatch")
        evidence[name] = {"status": "UNTRUSTED_PRODUCER", "path": str(path), "sha256": actual}

    # The repository has no protected WF/OOS evaluator registry yet. Evidence
    # can be hashed and retained, but no caller-authored artifact may unlock ACCEPTED.
    reasons.append("TRUSTED_EVALUATOR_UNAVAILABLE")
    baseline_sha, baseline_commit = _strategy_from_git(repo_root, baseline_ref)
    decision = "PROPOSED_ONLY" if candidate_info is not None else "NOT_APPLICABLE"
    public_ledger = {key: value for key, value in ledger.items() if key != "_events"}
    return {
        "schema_version": 1,
        "decision": decision,
        "reasons": sorted(set(reasons)),
        "run_id": proposal["run_id"],
        "legacy_acceptance_status": "INVALID_LEGACY_ACCEPTANCE",
        "claim_boundary": {
            "ledger_integrity": "VERIFIED_GIT_HEAD_PREFIX_AND_CURRENT_CHAIN",
            "closed_round_trips": "DERIVED_FROM_VALIDATED_PENDING_TO_FILLED_FSM",
            "eligible_samples": "REQUIRE_DECLARED_HASH_BOUND_PROVENANCE_UNTRUSTED_UNTIL_EVALUATOR_EXISTS",
            "walk_forward": "NOT_VERIFIED",
            "independent_oos": "NOT_VERIFIED",
            "rollback_replay": "NOT_VERIFIED",
            "accepted_path": "DISABLED_UNTIL_TRUSTED_EVALUATOR_EXISTS",
            "simulation_only": True,
        },
        "ledger": {
            **public_ledger,
            "sha256": sha256_file(ledger_path),
            "completed_round_trip_count": replay["completed_round_trip_count"],
            "eligible_sample_count": replay["eligible_sample_count"],
            "positions": replay["positions"],
            "portfolio_projection": "PASS",
        },
        "baseline": {"commit": baseline_commit, "strategy_sha256": baseline_sha},
        "candidate": candidate_info,
        "evidence": evidence,
        "verifier": {
            "source_sha256": sha256_file(Path(__file__)),
            "head_commit": _git_output(repo_root, "rev-parse", "HEAD"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "source_sha256": {
            "proposal": sha256_file(proposal_path),
            "anchor": sha256_file(anchor_path),
            "mode_lock": sha256_file(mode_lock_path),
            "portfolio": sha256_file(portfolio_path),
        },
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
