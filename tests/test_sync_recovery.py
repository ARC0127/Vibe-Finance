from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_github.sh"


class SyncRecoveryBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_status_is_restricted_to_manifest_safe_tokens(self) -> None:
        self.assertIn('[[ ! "$run_status" =~ ^[A-Z][A-Z0-9_]*$ ]]', self.source)
        self.assertIn(
            "status must use uppercase letters, digits, and underscores",
            self.source,
        )

    def test_remote_operations_use_bounded_retries(self) -> None:
        fetch_block = self.source.split("remote_fetch() {", 1)[1].split("}", 1)[0]
        push_block = self.source.split("remote_push() {", 1)[1].split("}", 1)[0]
        for block in (fetch_block, push_block):
            self.assertIn("for attempt in 1 2 3", block)
            self.assertIn("sleep $((attempt * 2))", block)
        self.assertNotIn('remote_git push -u origin "$branch"', self.source)

    def test_resume_requires_one_manifest_verified_commit(self) -> None:
        required_guards = (
            'git merge-base --is-ancestor "$remote_head" "$local_head"',
            'git rev-list --count "$remote_head..$local_head"',
            'manifest.get("task_id") != os.environ["RUN_TASK_ID"]',
            'manifest.get("task_status") != os.environ["RUN_STATUS"]',
            'manifest.get("branch") != os.environ["RUN_BRANCH"]',
            'manifest.get("base_commit") != base',
            'manifest.get("allowlist") != os.environ["RUN_ALLOWLIST"].split()',
            'changed_paths != expected_paths',
            'pending commit parent does not match origin',
            'pending-commit resume because current task paths are dirty',
        )
        for guard in required_guards:
            self.assertIn(guard, self.source)

    def test_resume_handles_git_line_ending_normalization(self) -> None:
        resume_block = self.source.split(
            'RUN_ALLOWLIST="${allowlist[*]}"', 1
        )[1].split("resume_pending_commit=true", 1)[0]
        self.assertIn("raw = Path(path).read_bytes()", resume_block)
        self.assertIn(
            '["git", "diff", "--quiet", head, "--", path]',
            resume_block,
        )
        self.assertNotIn("raw = committed(path)", resume_block)

    def test_every_real_push_is_followed_by_remote_sha_verification(self) -> None:
        self.assertEqual(self.source.count('remote_push -u origin "$branch"'), 2)
        self.assertEqual(self.source.count('remote_fetch --quiet origin "$branch"'), 2)
        self.assertEqual(
            self.source.count('if [[ "$pushed_head" != "$pushed_remote_head" ]]'),
            2,
        )
        self.assertEqual(self.source.count("push verification failed:"), 2)

    def test_windows_api_probe_uses_credential_manager_without_cli_secret(self) -> None:
        windows_probe = self.source.split("github_credential=$(", 1)[1].split(
            "else\n  command -v gh", 1
        )[0]
        self.assertIn("git.exe credential fill", windows_probe)
        self.assertIn("--config -", windows_probe)
        self.assertIn('unset github_token', windows_probe)
        self.assertNotIn('curl.exe -H "Authorization:', windows_probe)

    def test_governance_code_release_is_narrow_and_skips_readme_refresh(self) -> None:
        block = self.source.split("governance-code-release)", 1)[1].split(";;", 1)[0]
        self.assertIn(
            "allowlist=(config/task_contracts.json scripts/sync_github.sh tests vibe_finance/pipeline.py vibe_finance/task_contracts.py vibe_finance/transaction.py)",
            block,
        )
        self.assertNotIn("reports", block)
        self.assertNotIn("data/ledger", block)
        self.assertIn('"$task_id" != "governance-code-release"', self.source)

    def test_financial_sync_stages_task_owned_paths_only(self) -> None:
        self.assertIn("select_sync_owned_paths", self.source)
        self.assertIn('paths_to_stage=("${owned_paths[@]}")', self.source)
        self.assertNotIn('paths_to_stage=("${allowlist[@]}")', self.source)


if __name__ == "__main__":
    unittest.main()
