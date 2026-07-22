from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sync_github.sh"


class SyncEvolutionBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_reflection_allowlist_is_evidence_only(self):
        block = self.source.split("reflection-evolution)", 1)[1].split(";;", 1)[0]
        self.assertIn("allowlist=(reports/evolution)", block)
        for forbidden in ("MODE_LOCK", "scripts/sync_github.sh", "vibe_finance", "tests", "config/strategy"):
            self.assertNotIn(forbidden, block)

    def test_protected_paths_and_caller_decision_rejection_are_explicit(self):
        self.assertIn("refusing caller-supplied evolution decision or rollback base", self.source)
        self.assertIn("scripts/sync_github.sh vibe_finance tests config data/ledger", self.source)
        self.assertIn("ACCEPTED is disabled until a protected trusted-evaluator registry exists", self.source)

    def test_manifest_uses_only_pinned_gate_values(self):
        manifest = self.source.split("manifest = {", 1)[1].split(
            'path = Path(os.environ["RUN_MANIFEST"])', 1
        )[0]
        self.assertNotIn("gate.get(", manifest)
        self.assertIn('"evolution_decision": os.environ["EVOLUTION_DECISION"]', manifest)
        self.assertIn('"sha256": os.environ["EVOLUTION_GATE_SHA"]', manifest)
        self.assertNotIn('os.environ.get("VIBE_EVOLUTION_DECISION"', manifest)

    def test_gate_and_referenced_artifacts_are_reverified_after_staging(self):
        self.assertIn("verify_evolution_worktree_contract", self.source)
        self.assertIn("staged evolution gate differs from the pinned verifier result", self.source)
        self.assertIn("staged referenced artifact differs from its gate hash", self.source)
        self.assertIn("staged manifest is not bound to the pinned evolution gate", self.source)
        self.assertIn('paths_to_stage=("${evolution_allowed_paths[@]}")', self.source)


if __name__ == "__main__":
    unittest.main()
