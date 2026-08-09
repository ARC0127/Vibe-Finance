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

    def test_every_real_push_is_followed_by_remote_sha_verification(self) -> None:
        self.assertEqual(self.source.count('remote_push -u origin "$branch"'), 2)
        self.assertEqual(self.source.count('remote_fetch --quiet origin "$branch"'), 2)
        self.assertEqual(
            self.source.count('if [[ "$pushed_head" != "$pushed_remote_head" ]]'),
            2,
        )
        self.assertEqual(self.source.count("push verification failed:"), 2)

    def test_governance_code_release_is_narrow_and_skips_readme_refresh(self) -> None:
        block = self.source.split("governance-code-release)", 1)[1].split(";;", 1)[0]
        self.assertIn(
            "allowlist=(config/task_contracts.json scripts/sync_github.sh tests vibe_finance/pipeline.py)",
            block,
        )
        self.assertNotIn("reports", block)
        self.assertNotIn("data/ledger", block)
        self.assertIn('"$task_id" != "governance-code-release"', self.source)


if __name__ == "__main__":
    unittest.main()
