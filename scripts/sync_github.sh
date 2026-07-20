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
    allowlist=(reports/monitor)
    ;;
  preopen-review)
    allowlist=(reports/preopen reports/research data/research)
    ;;
  open-settlement)
    allowlist=(reports/execution data/inbox data/ledger)
    ;;
  close-analysis)
    allowlist=(reports/daily reports/research data/inbox data/research data/ledger)
    ;;
  fund-nav)
    allowlist=(reports/funds data/inbox data/ledger)
    ;;
  reflection-evolution)
    allowlist=(reports/evolution config docs MASTER_PROMPT.md MODE_LOCK.md MODE_LOCK.json vibe_finance tests)
    ;;
  weekly-review)
    allowlist=(reports/weekly reports/daily data/research data/ledger)
    ;;
  document-log)
    allowlist=(reports/document-log docs/DOCUMENT_LOG_INDEX.md docs/GITHUB_AUTOMATION.md README.md MODE_LOCK.md MODE_LOCK.json docs/SOURCES.md)
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

command -v gh >/dev/null
gh auth status >/dev/null

branch=$(git branch --show-current)
if [[ "$branch" != "$expected_branch" ]]; then
  echo "refusing sync from branch '$branch'; expected '$expected_branch'" >&2
  exit 4
fi

git fetch --quiet origin "$expected_branch"
local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse "origin/$expected_branch")
if [[ "$local_head" != "$remote_head" ]]; then
  echo "refusing sync because local $expected_branch ($local_head) differs from origin/$expected_branch ($remote_head)" >&2
  echo "resolve with an explicit fast-forward or conflict review before retrying" >&2
  exit 4
fi

read -r visibility permission < <(
  gh repo view ARC0127/Vibe-Finance --json visibility,viewerPermission \
    --jq '[.visibility, .viewerPermission] | @tsv'
)
if [[ "$visibility" != "PRIVATE" ]]; then
  echo "refusing sync because repository visibility is '$visibility'" >&2
  exit 5
fi
if [[ "$permission" != "ADMIN" && "$permission" != "WRITE" ]]; then
  echo "refusing sync because viewer permission is '$permission'" >&2
  exit 5
fi

if ! git diff --cached --quiet; then
  echo "refusing sync because the Git index already contains staged changes" >&2
  exit 6
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

manifest = {
    "schema_version": 1,
    "task_id": os.environ["TASK_ID"],
    "task_status": os.environ["RUN_STATUS"],
    "run_timestamp": os.environ["RUN_TIMESTAMP"],
    "run_id": os.environ.get("VIBE_RUN_ID", "UNKNOWN"),
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
    "evolution_decision": os.environ.get("VIBE_EVOLUTION_DECISION", "NOT_APPLICABLE"),
    "rollback_base": os.environ.get("VIBE_ROLLBACK_BASE", "NOT_APPLICABLE"),
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
for path in "${allowlist[@]}"; do
  if [[ -e "$path" ]]; then
    stage_paths+=("$path")
  fi
done
git add -- "${stage_paths[@]}"

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

commit_message="automation($task_id): $timestamp_human $run_status"
git commit -m "$commit_message"
git push -u origin "$branch"

printf 'sync=PASS\ntask_id=%s\nmanifest=%s\ncommit=%s\nbranch=%s\n' \
  "$task_id" "$manifest" "$(git rev-parse HEAD)" "$branch"
