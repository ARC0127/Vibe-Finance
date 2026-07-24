#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <task-id> <status> [--dry-run]" >&2
  exit 2
}

task_id=${1:-}
run_status=${2:-}
mode=${3:-}
[[ -n "$task_id" && -n "$run_status" ]] || usage
[[ -z "$mode" || "$mode" == "--dry-run" ]] || usage

expected_branch=${VIBE_FINANCE_SYNC_BRANCH:-main}
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

case "$task_id" in
  activity-monitor)
    allowlist=(reports/monitor README.md)
    ;;
  preopen-review)
    allowlist=(reports/preopen reports/research data/inbox data/research data/ledger README.md)
    ;;
  daily-order-guard)
    allowlist=(reports/preopen reports/research data/inbox data/research data/ledger README.md)
    ;;
  open-settlement)
    allowlist=(reports/execution data/inbox data/ledger README.md)
    ;;
  close-analysis)
    allowlist=(reports/daily reports/research data/inbox data/research data/ledger README.md)
    ;;
  fund-nav)
    allowlist=(reports/funds data/inbox data/ledger README.md)
    ;;
  reflection-evolution)
    allowlist=(reports/evolution)
    ;;
  weekly-review)
    allowlist=(reports/weekly reports/daily data/research data/ledger artifacts/weekly-dashboard README.md)
    ;;
  document-log)
    allowlist=(reports/document-log docs/DOCUMENT_LOG_INDEX.md docs/GITHUB_AUTOMATION.md README.md MODE_LOCK.md MODE_LOCK.json docs/SOURCES.md)
    ;;
  skill-memory-review)
    allowlist=(reports/skill-memory/reviews)
    ;;
  current-period-release)
    allowlist=(.gitignore MASTER_PROMPT.md MODE_LOCK.md MODE_LOCK.json README.md config data docs reports scripts tests vibe_finance)
    ;;
  *)
    echo "unknown task-id: $task_id" >&2
    exit 2
    ;;
esac

mkdir -p "$HOME/.cache/vibe-finance"
exec 9>"$HOME/.cache/vibe-finance/github-sync.lock"
if ! flock -w 300 9; then
  echo "could not acquire GitHub sync lock within 300 seconds" >&2
  exit 3
fi

export GH_PROMPT_DISABLED=1
export GIT_TERMINAL_PROMPT=0

# WSL may not inherit the Windows desktop proxy. Prefer Windows Git/curl when
# available: they can reach the same checkout and use Git Credential Manager.
windows_proxy=${VIBE_FINANCE_WINDOWS_PROXY:-http://127.0.0.1:7890}
use_windows_transport=false
if command -v git.exe >/dev/null && command -v curl.exe >/dev/null; then
  use_windows_transport=true
fi

remote_git() {
  if [[ "$use_windows_transport" == true ]]; then
    env HTTP_PROXY="$windows_proxy" HTTPS_PROXY="$windows_proxy" ALL_PROXY= git.exe "$@"
  else
    git "$@"
  fi
}

branch=$(git branch --show-current)
if [[ "$branch" != "$expected_branch" ]]; then
  echo "refusing sync from branch '$branch'; expected '$expected_branch'" >&2
  exit 4
fi

remote_git fetch --quiet origin "$expected_branch"
local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse "origin/$expected_branch")
if [[ "$local_head" != "$remote_head" ]]; then
  echo "refusing sync because local $expected_branch ($local_head) differs from origin/$expected_branch ($remote_head)" >&2
  echo "resolve with an explicit fast-forward or conflict review before retrying" >&2
  exit 4
fi

if [[ "$use_windows_transport" == true ]]; then
  repo_json=$(
    env HTTP_PROXY="$windows_proxy" HTTPS_PROXY="$windows_proxy" ALL_PROXY= \
      curl.exe -fsS --max-time 30 https://api.github.com/repos/ARC0127/Vibe-Finance
  )
  visibility=$(printf '%s' "$repo_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["visibility"].upper())')
  remote_git push --dry-run origin "$expected_branch" >/dev/null
  permission="WRITE_VERIFIED_BY_DRY_RUN"
else
  command -v gh >/dev/null
  gh auth status >/dev/null
  read -r visibility permission < <(
    gh repo view ARC0127/Vibe-Finance --json visibility,viewerPermission \
      --jq '[.visibility, .viewerPermission] | @tsv'
  )
fi
if [[ "$visibility" != "PRIVATE" && "$visibility" != "PUBLIC" ]]; then
  echo "refusing sync because repository visibility '$visibility' is unsupported" >&2
  exit 5
fi
if [[ "$permission" != "ADMIN" && "$permission" != "WRITE" && "$permission" != "WRITE_VERIFIED_BY_DRY_RUN" ]]; then
  echo "refusing sync because viewer permission is '$permission'" >&2
  exit 5
fi

if ! git diff --cached --quiet; then
  echo "refusing sync because the Git index already contains staged changes" >&2
  exit 6
fi

evolution_gate=""
evolution_gate_sha="NOT_APPLICABLE"
evolution_decision="NOT_APPLICABLE"
evolution_rollback_base="NOT_APPLICABLE"
evolution_run_id="NOT_APPLICABLE"
evolution_allowed_paths=()
if [[ "$task_id" == "reflection-evolution" ]]; then
  if [[ -n "${VIBE_EVOLUTION_DECISION:-}" || -n "${VIBE_ROLLBACK_BASE:-}" ]]; then
    echo "refusing caller-supplied evolution decision or rollback base" >&2
    exit 6
  fi
  protected_paths=(
    MASTER_PROMPT.md MODE_LOCK.md MODE_LOCK.json README.md docs pyproject.toml
    scripts/sync_github.sh vibe_finance tests config data/ledger
  )
  protected_dirty=$(git status --porcelain=v1 --untracked-files=all -- "${protected_paths[@]}")
  if [[ -n "$protected_dirty" ]]; then
    echo "refusing reflection because protected paths are dirty:" >&2
    echo "$protected_dirty" >&2
    exit 6
  fi
  proposal=${VIBE_EVOLUTION_PROPOSAL:-}
  if [[ -z "$proposal" ]]; then
    echo "reflection requires VIBE_EVOLUTION_PROPOSAL under reports/evolution/<run-id>/proposal.json" >&2
    exit 6
  fi
  proposal=$(PROPOSAL="$proposal" python3 - <<'PY'
import os
from pathlib import Path

root = Path("reports/evolution").resolve()
proposal = Path(os.environ["PROPOSAL"])
if proposal.is_absolute() or ".." in proposal.parts:
    raise SystemExit("proposal path must be relative and cannot contain '..'")
resolved = proposal.resolve(strict=True)
if root not in resolved.parents or resolved.name != "proposal.json":
    raise SystemExit("proposal must be reports/evolution/<run-id>/proposal.json")
print(resolved.relative_to(Path.cwd().resolve()).as_posix())
PY
  )
  proposal_dir=$(dirname "$proposal")
  evolution_gate="$proposal_dir/gate.json"
  if [[ "$mode" == "--dry-run" ]]; then
    evolution_json=$(python3 -m vibe_finance evolution-gate \
      --proposal "$proposal" \
      --baseline-ref "$local_head")
  else
    evolution_json=$(python3 -m vibe_finance evolution-gate \
      --proposal "$proposal" \
      --baseline-ref "$local_head" \
      --output "$evolution_gate")
  fi
  read -r evolution_decision evolution_rollback_base evolution_run_id evolution_gate_sha < <(
    EVOLUTION_JSON="$evolution_json" EVOLUTION_GATE="$evolution_gate" RUN_MODE="$mode" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

value = json.loads(os.environ["EVOLUTION_JSON"])
gate_sha = "NOT_APPLICABLE"
if os.environ["RUN_MODE"] != "--dry-run":
    gate_path = Path(os.environ["EVOLUTION_GATE"])
    raw = gate_path.read_bytes()
    gate = json.loads(raw)
    if gate != value:
        raise SystemExit("gate file differs from the verifier result captured on stdout")
    gate_sha = hashlib.sha256(raw).hexdigest()
print(value["decision"], value["baseline"]["commit"], value["run_id"], gate_sha)
PY
  )
  case "$evolution_decision" in
    PROPOSED_ONLY|REJECTED|NOT_APPLICABLE)
      ;;
    ACCEPTED)
      echo "ACCEPTED is disabled until a protected trusted-evaluator registry exists" >&2
      exit 6
      ;;
    *)
      echo "refusing unknown evolution decision: $evolution_decision" >&2
      exit 6
      ;;
  esac

  mapfile -t evolution_allowed_paths < <(
    EVOLUTION_JSON="$evolution_json" PROPOSAL="$proposal" EVOLUTION_GATE="$evolution_gate" \
      RUN_MODE="$mode" python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path.cwd().resolve()
run_dir = Path(os.environ["PROPOSAL"]).resolve().parent
value = json.loads(os.environ["EVOLUTION_JSON"])
paths = [Path(os.environ["PROPOSAL"]).resolve()]
if os.environ["RUN_MODE"] != "--dry-run":
    paths.append(Path(os.environ["EVOLUTION_GATE"]).resolve())
candidate = value.get("candidate")
if candidate:
    paths.append(Path(candidate["path"]).resolve())
for evidence in value.get("evidence", {}).values():
    if evidence.get("path"):
        paths.append(Path(evidence["path"]).resolve())
for path in paths:
    if path.parent != run_dir or not path.is_file() or path.is_symlink():
        raise SystemExit(f"evolution artifact must be a regular file directly under the unique run directory: {path}")
    print(path.relative_to(root).as_posix())
PY
  )

  while IFS= read -r record; do
    [[ -z "$record" ]] && continue
    status=${record:0:2}
    if [[ "$status" != "??" ]]; then
      echo "reflection may only add a new evidence run; historical evolution artifacts are immutable: $record" >&2
      exit 6
    fi
    artifact=${record:3}
    permitted=false
    for expected in "${evolution_allowed_paths[@]}"; do
      if [[ "$artifact" == "$expected" ]]; then
        permitted=true
        break
      fi
    done
    if [[ "$permitted" != true ]]; then
      echo "reflection contains an unreferenced or extra evolution artifact: $artifact" >&2
      exit 6
    fi
  done < <(git status --porcelain=v1 --untracked-files=all -- reports/evolution)
  for expected in "${evolution_allowed_paths[@]}"; do
    if [[ "$(git status --porcelain=v1 --untracked-files=all -- "$expected")" != "?? $expected" ]]; then
      echo "reflection artifact is not a unique untracked file: $expected" >&2
      exit 6
    fi
  done
fi

verify_evolution_worktree_contract() {
  EVOLUTION_GATE="$evolution_gate" \
  EVOLUTION_GATE_SHA="$evolution_gate_sha" \
  EVOLUTION_DECISION="$evolution_decision" \
  EVOLUTION_ROLLBACK_BASE="$evolution_rollback_base" \
  EVOLUTION_RUN_ID="$evolution_run_id" \
  PROPOSAL="$proposal" \
  python3 - <<'PY'
import hashlib
import json
import os
import subprocess
from pathlib import Path

root = Path.cwd().resolve()

def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

gate_path = Path(os.environ["EVOLUTION_GATE"])
gate_raw = gate_path.read_bytes()
if digest(gate_raw) != os.environ["EVOLUTION_GATE_SHA"]:
    raise SystemExit("evolution gate SHA drifted after verification")
gate = json.loads(gate_raw)
if gate["decision"] != os.environ["EVOLUTION_DECISION"]:
    raise SystemExit("evolution gate decision drifted after verification")
if gate["baseline"]["commit"] != os.environ["EVOLUTION_ROLLBACK_BASE"]:
    raise SystemExit("evolution rollback base drifted after verification")
if gate["run_id"] != os.environ["EVOLUTION_RUN_ID"]:
    raise SystemExit("evolution run_id drifted after verification")

proposal_path = Path(os.environ["PROPOSAL"]).resolve()
if digest(proposal_path.read_bytes()) != gate["source_sha256"]["proposal"]:
    raise SystemExit("proposal SHA drifted after gate computation")
references = []
if gate.get("candidate"):
    references.append((Path(gate["candidate"]["path"]), gate["candidate"]["sha256"]))
for value in gate.get("evidence", {}).values():
    if value.get("path"):
        references.append((Path(value["path"]), value["sha256"]))
for path, expected in references:
    if path.resolve().parent != proposal_path.parent or digest(path.read_bytes()) != expected:
        raise SystemExit(f"referenced evolution artifact drifted: {path}")

protected = {
    "config/ledger_legacy_anchor.json": gate["source_sha256"]["anchor"],
    "MODE_LOCK.json": gate["source_sha256"]["mode_lock"],
    "data/ledger/portfolio.json": gate["source_sha256"]["portfolio"],
    "vibe_finance/evolution.py": gate["verifier"]["source_sha256"],
}
for relative, expected in protected.items():
    working = (root / relative).read_bytes()
    committed = subprocess.check_output(["git", "show", f"HEAD:{relative}"])
    if digest(working) != expected or digest(committed) != expected:
        raise SystemExit(f"protected gate authority is not pinned to HEAD: {relative}")
PY
}

if [[ "$task_id" == "reflection-evolution" && "$mode" != "--dry-run" ]]; then
  verify_evolution_worktree_contract
fi

# Keep the public status block ledger-derived for operational tasks. Reflection
# owns proposal evidence only and must not touch README or any live projection.
if [[ "$task_id" != "reflection-evolution" ]]; then
  python3 -m vibe_finance update-readme >/dev/null
fi

deleted=$(git status --porcelain=v1 --untracked-files=all -- "${allowlist[@]}" | awk 'substr($0,1,2) ~ /D/ {print}')
if [[ -n "$deleted" ]]; then
  echo "refusing automatic deletion or rename:" >&2
  echo "$deleted" >&2
  exit 7
fi

secret_matches=$(rg -l \
  '(sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})' \
  --hidden -g '!.git/**' -g '!*.pyc' . || true)
if [[ -n "$secret_matches" ]]; then
  echo "refusing sync because a credential-like value was found" >&2
  echo "$secret_matches" >&2
  exit 8
fi

python3 - <<'PY'
import json
from pathlib import Path

for path in Path(".").rglob("*.json"):
    if ".git" not in path.parts:
        json.loads(path.read_text(encoding="utf-8"))
for path in Path(".").rglob("*.jsonl"):
    if ".git" in path.parts:
        continue
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            json.loads(line)
PY

git diff --check
python3 -m unittest discover -s tests -q

if [[ "$task_id" == "reflection-evolution" && "$mode" != "--dry-run" ]]; then
  verify_evolution_worktree_contract
fi

if [[ "$mode" == "--dry-run" ]]; then
  printf 'dry_run=PASS\ntask_id=%s\nbranch=%s\nvisibility=%s\npermission=%s\n' \
    "$task_id" "$branch" "$visibility" "$permission"
  exit 0
fi

timestamp=$(date '+%Y%m%dT%H%M%S%z')
timestamp_human=$(date '+%Y-%m-%d %H:%M:%S %Z')
manifest_dir="reports/automation-runs/$task_id"
manifest="$manifest_dir/$timestamp.json"
mkdir -p "$manifest_dir"

TASK_ID="$task_id" \
RUN_STATUS="$run_status" \
RUN_TIMESTAMP="$timestamp_human" \
RUN_BRANCH="$branch" \
RUN_MANIFEST="$manifest" \
RUN_ALLOWLIST="${allowlist[*]}" \
EVOLUTION_GATE="$evolution_gate" \
EVOLUTION_GATE_SHA="$evolution_gate_sha" \
EVOLUTION_DECISION="$evolution_decision" \
EVOLUTION_ROLLBACK_BASE="$evolution_rollback_base" \
EVOLUTION_RUN_ID="$evolution_run_id" \
python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

allowlist = os.environ["RUN_ALLOWLIST"].split()
status_raw = subprocess.check_output(
    ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *allowlist]
).decode("utf-8", errors="replace")
records = [item for item in status_raw.split("\0") if item]
files = []
for record in records:
    path_text = record[3:]
    if " -> " in path_text:
        path_text = path_text.split(" -> ", 1)[1]
    path = Path(path_text)
    item = {"git_status": record[:2], "path": path.as_posix()}
    if path.is_file():
        raw = path.read_bytes()
        item.update(
            {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    files.append(item)

ledger_path = Path("data/ledger/portfolio.json")
ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
strategy_path = Path("config/strategy.json")
strategy = json.loads(strategy_path.read_text(encoding="utf-8")) if strategy_path.exists() else {}
infra = ledger.get("research_infrastructure", {})
gate_path = Path(os.environ["EVOLUTION_GATE"]) if os.environ.get("EVOLUTION_GATE") else None

manifest = {
    "schema_version": 1,
    "task_id": os.environ["TASK_ID"],
    "task_status": os.environ["RUN_STATUS"],
    "run_timestamp": os.environ["RUN_TIMESTAMP"],
    "run_id": (
        os.environ["EVOLUTION_RUN_ID"]
        if os.environ["EVOLUTION_RUN_ID"] != "NOT_APPLICABLE"
        else os.environ.get("VIBE_RUN_ID", "UNKNOWN")
    ),
    "branch": os.environ["RUN_BRANCH"],
    "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "allowlist": allowlist,
    "changed_files_before_manifest": files,
    "validation": {
        "secret_scan": "PASS",
        "json_jsonl_parse": "PASS",
        "git_diff_check": "PASS",
        "unit_tests": "PASS",
    },
    "strategy_version": strategy.get("version", "UNKNOWN"),
    "evolution_decision": os.environ["EVOLUTION_DECISION"],
    "rollback_base": os.environ["EVOLUTION_ROLLBACK_BASE"],
    "evolution_gate": (
        {
            "path": gate_path.as_posix(),
            "sha256": os.environ["EVOLUTION_GATE_SHA"],
        }
        if gate_path and gate_path.exists()
        else None
    ),
    "deepseek": {
        "actual_calls": infra.get("actual_calls", 0),
        "spent_cny": infra.get("spent_cny", 0),
        "reserved_cny": infra.get("reserved_cny", 0),
    },
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "simulation_only": True,
}
path = Path(os.environ["RUN_MANIFEST"])
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)
PY

stage_paths=("$manifest")
if [[ "$task_id" == "reflection-evolution" ]]; then
  paths_to_stage=("${evolution_allowed_paths[@]}")
else
  paths_to_stage=("${allowlist[@]}")
fi
for path in "${paths_to_stage[@]}"; do
  if [[ -e "$path" ]]; then
    stage_paths+=("$path")
  fi
done
git add -- "${stage_paths[@]}"
staged_tree_after_add=$(git write-tree)

if [[ "$task_id" == "reflection-evolution" ]]; then
  printf -v evolution_allowed_paths_lines '%s\n' "${evolution_allowed_paths[@]}"
  EVOLUTION_GATE="$evolution_gate" \
  EVOLUTION_GATE_SHA="$evolution_gate_sha" \
  EVOLUTION_DECISION="$evolution_decision" \
  EVOLUTION_ROLLBACK_BASE="$evolution_rollback_base" \
  EVOLUTION_RUN_ID="$evolution_run_id" \
  EVOLUTION_ALLOWED_PATHS="$evolution_allowed_paths_lines" \
  RUN_MANIFEST="$manifest" \
  python3 - <<'PY'
import hashlib
import json
import os
import subprocess
from pathlib import Path

root = Path.cwd().resolve()

def staged(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f":{path}"])

def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

allowed = set(os.environ["EVOLUTION_ALLOWED_PATHS"].splitlines())
gate_path = os.environ["EVOLUTION_GATE"]
if gate_path not in allowed:
    raise SystemExit("pinned gate is absent from the exact staging contract")
gate_raw = staged(gate_path)
if digest(gate_raw) != os.environ["EVOLUTION_GATE_SHA"]:
    raise SystemExit("staged evolution gate differs from the pinned verifier result")
gate = json.loads(gate_raw)
if (
    gate["decision"] != os.environ["EVOLUTION_DECISION"]
    or gate["baseline"]["commit"] != os.environ["EVOLUTION_ROLLBACK_BASE"]
    or gate["run_id"] != os.environ["EVOLUTION_RUN_ID"]
):
    raise SystemExit("staged gate authority differs from pinned control-flow values")

proposal_path = next(path for path in allowed if path.endswith("/proposal.json"))
if digest(staged(proposal_path)) != gate["source_sha256"]["proposal"]:
    raise SystemExit("staged proposal differs from its gate hash")
references = []
if gate.get("candidate"):
    references.append((gate["candidate"]["path"], gate["candidate"]["sha256"]))
for value in gate.get("evidence", {}).values():
    if value.get("path"):
        references.append((value["path"], value["sha256"]))
for raw_path, expected in references:
    path = Path(raw_path).resolve().relative_to(root).as_posix()
    if path not in allowed or digest(staged(path)) != expected:
        raise SystemExit(f"staged referenced artifact differs from its gate hash: {path}")

manifest = json.loads(staged(os.environ["RUN_MANIFEST"]))
if (
    manifest["evolution_decision"] != os.environ["EVOLUTION_DECISION"]
    or manifest["rollback_base"] != os.environ["EVOLUTION_ROLLBACK_BASE"]
    or manifest["run_id"] != os.environ["EVOLUTION_RUN_ID"]
    or manifest["evolution_gate"] != {
        "path": gate_path,
        "sha256": os.environ["EVOLUTION_GATE_SHA"],
    }
):
    raise SystemExit("staged manifest is not bound to the pinned evolution gate")
PY
fi

mapfile -t staged_paths < <(git diff --cached --name-only)
if [[ ${#staged_paths[@]} -eq 0 ]]; then
  echo "no staged files after creating run manifest" >&2
  exit 9
fi

for staged in "${staged_paths[@]}"; do
  allowed=false
  if [[ "$staged" == "$manifest" ]]; then
    allowed=true
  else
    for prefix in "${allowlist[@]}"; do
      if [[ "$staged" == "$prefix" || "$staged" == "$prefix/"* ]]; then
        allowed=true
        break
      fi
    done
  fi
  if [[ "$allowed" != true ]]; then
    echo "refusing staged path outside task allowlist: $staged" >&2
    exit 10
  fi
done

if [[ "$(git write-tree)" != "$staged_tree_after_add" ]]; then
  echo "refusing commit because the staged tree changed during verification" >&2
  exit 10
fi

commit_message="automation($task_id): $timestamp_human $run_status"
git commit -m "$commit_message"
remote_git push -u origin "$branch"

printf 'sync=PASS\ntask_id=%s\nmanifest=%s\ncommit=%s\nbranch=%s\n' \
  "$task_id" "$manifest" "$(git rev-parse HEAD)" "$branch"
