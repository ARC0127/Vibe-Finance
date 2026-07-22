from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_finance.evolution import (
    DEFAULT_ANCHOR,
    DEFAULT_LEDGER,
    DEFAULT_MODE_LOCK,
    DEFAULT_PORTFOLIO,
    GENESIS_EVENT_SHA256,
    REPO_ROOT,
    EvolutionGateError,
    append_order_event,
    canonical_payload,
    derive_completed_round_trips,
    event_sha256,
    legacy_chain_head,
    pipeline_event_payload_sha256,
    sha256_bytes,
    sha256_file,
    verify_event_ledger,
    verify_evolution_gate,
    verify_portfolio_projection,
    verify_pipeline_event_provenance,
)


BASELINE_REF = "3950ea74801c28a63fd99ba3c6a9e7fe3e2cc6e5"


def _fill(order_id: str, side: str, quantity: str | int, symbol: str = "TEST") -> dict:
    return {
        "order_id": order_id,
        "status": "FILLED",
        "side": side,
        "symbol": symbol,
        "quantity": quantity,
        "simulation_only": True,
        "fill_as_of": "2026-07-21T10:01:00+08:00",
        "run_id": f"fill-{order_id}",
    }


def _pending(order_id: str, side: str, quantity: str | int, symbol: str = "TEST") -> dict:
    return {
        "order_id": order_id,
        "status": "PENDING_NEXT_OPEN",
        "side": side,
        "symbol": symbol,
        "quantity": quantity,
        "simulation_only": True,
        "signal_as_of": "2026-07-21T10:00:00+08:00",
        "run_id": f"pending-{order_id}",
    }


def _lifecycle(order_id: str, side: str, quantity: str | int, symbol: str = "TEST") -> list[dict]:
    return [_pending(order_id, side, quantity, symbol), _fill(order_id, side, quantity, symbol)]


def _make_anchored_ledger(root: Path) -> tuple[Path, Path]:
    ledger = root / "data/ledger/orders.jsonl"
    ledger.parent.mkdir(parents=True)
    production_anchor = json.loads(DEFAULT_ANCHOR.read_text(encoding="utf-8"))
    legacy_line_count = int(production_anchor["legacy_line_count"])
    production_lines = DEFAULT_LEDGER.read_text(encoding="utf-8").splitlines()
    ledger.write_text(
        "\n".join(production_lines[:legacy_line_count]) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Vibe Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "data/ledger/orders.jsonl"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "anchor ledger"], check=True)
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    blob = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD:data/ledger/orders.jsonl"], text=True
    ).strip()
    anchor = root / "ledger_legacy_anchor.json"
    anchor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ledger_path": "data/ledger/orders.jsonl",
                "source_commit": commit,
                "source_blob_oid": blob,
                "legacy_line_count": len(events),
                "canonical_payload_sha256": sha256_bytes(canonical_payload(events)),
                "chain_head_sha256": legacy_chain_head(events),
                "policy": "LEGACY_GIT_ANCHORED_READ_ONLY_PREFIX_V1",
            }
        ),
        encoding="utf-8",
    )
    return ledger, anchor


class EvolutionLedgerTests(unittest.TestCase):
    def test_missing_repo_git_fails_closed_before_parent_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "data/ledger/orders.jsonl"
            ledger.parent.mkdir(parents=True)
            production_anchor = json.loads(DEFAULT_ANCHOR.read_text(encoding="utf-8"))
            legacy_line_count = int(production_anchor["legacy_line_count"])
            ledger.write_text(
                "\n".join(DEFAULT_LEDGER.read_text(encoding="utf-8").splitlines()[:legacy_line_count])
                + "\n",
                encoding="utf-8",
            )
            anchor = root / "ledger_legacy_anchor.json"
            anchor.write_text(
                json.dumps(production_anchor, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with mock.patch("vibe_finance.evolution.subprocess.run") as run_mock:
                with self.assertRaisesRegex(EvolutionGateError, "GIT_METADATA_UNAVAILABLE"):
                    verify_event_ledger(ledger, anchor, root)
            run_mock.assert_not_called()

    def test_schema_v2_provenance_requires_git_bound_exact_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Vibe Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "config",
                    "user.email",
                    "test@example.invalid",
                ],
                check=True,
            )
            input_path = root / "input.json"
            strategy_path = root / "strategy.json"
            decision_path = root / "decision.json"
            input_path.write_text('{"value":1}\n', encoding="utf-8")
            strategy_path.write_text('{"version":"test"}\n', encoding="utf-8")
            event = _pending("git-bound", "BUY", 1)
            event_core = {
                key: value for key, value in event.items() if key != "run_id"
            }
            decision_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": event["run_id"],
                        "mode": "short",
                        "input_sha256": sha256_file(input_path),
                        "strategy_sha256": sha256_file(strategy_path),
                        "new_orders": [event_core],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "bind artifacts"],
                check=True,
            )
            event["evolution_provenance"] = {
                "schema_version": 2,
                "producer": "vibe_finance.pipeline",
                "pipeline_mode": "short",
                "event_payload_sha256": pipeline_event_payload_sha256(event),
                "input_path": "input.json",
                "input_sha256": sha256_file(input_path),
                "strategy_path": "strategy.json",
                "strategy_sha256": sha256_file(strategy_path),
                "decision_manifest_path": "decision.json",
                "decision_manifest_sha256": sha256_file(decision_path),
            }
            self.assertTrue(verify_pipeline_event_provenance(event, root))
            event["quantity"] = 2
            self.assertFalse(verify_pipeline_event_provenance(event, root))

    def test_nav_buy_amount_contract_replays_fractional_confirmed_shares(self):
        pending_buy = {
            "order_id": "nav-buy",
            "status": "PENDING_NEXT_NAV",
            "side": "BUY",
            "symbol": "FUND",
            "amount_cny": 1000.0,
            "share_precision": 4,
            "fund_share_rounding": "ROUND_HALF_UP",
            "simulation_only": True,
            "signal_as_of": "2026-07-17T22:30:00+08:00",
            "run_id": "pending-nav-buy",
        }
        fill_buy = {
            "order_id": "nav-buy",
            "status": "FILLED",
            "side": "BUY",
            "symbol": "FUND",
            "amount_cny": 1000.0,
            "gross_amount_cny": 1000.0,
            "confirmed_shares": "895.3411",
            "fill_nav": 1.116,
            "fund_fee_cny": 0.80,
            "purchase_fee_rate_applied": "0.0008",
            "share_precision": 4,
            "fund_share_rounding": "ROUND_HALF_UP",
            "simulation_only": True,
            "fill_as_of": "2026-07-18T22:30:00+08:00",
            "run_id": "fill-nav-buy",
        }
        pending_sell = {
            "order_id": "nav-sell",
            "status": "PENDING_NEXT_NAV",
            "side": "SELL",
            "symbol": "FUND",
            "quantity": "895.3411",
            "share_precision": 4,
            "fund_share_rounding": "ROUND_HALF_UP",
            "simulation_only": True,
            "signal_as_of": "2026-07-19T22:30:00+08:00",
            "run_id": "pending-nav-sell",
        }
        fill_sell = {
            "order_id": "nav-sell",
            "status": "FILLED",
            "side": "SELL",
            "symbol": "FUND",
            "confirmed_shares": "895.3411",
            "fill_nav": 1.116,
            "gross_amount_cny": 999.20,
            "fund_fee_cny": 14.99,
            "redemption_fee_rate_applied": "0.015",
            "share_precision": 4,
            "fund_share_rounding": "ROUND_HALF_UP",
            "simulation_only": True,
            "fill_as_of": "2026-07-20T22:30:00+08:00",
            "run_id": "fill-nav-sell",
        }
        replay = derive_completed_round_trips(
            [pending_buy, fill_buy, pending_sell, fill_sell]
        )
        self.assertEqual(replay["filled_event_count"], 2)
        self.assertEqual(replay["completed_round_trip_count"], 1)
        self.assertEqual(replay["eligible_sample_count"], 0)
        self.assertEqual(replay["positions"], {})

        tampered = dict(fill_buy)
        tampered["confirmed_shares"] = "895.3412"
        with self.assertRaisesRegex(EvolutionGateError, "NAV share formula"):
            derive_completed_round_trips([pending_buy, tampered])

    def test_schema_v1_provenance_shaped_hashes_are_never_eligible(self):
        events = _lifecycle("buy", "BUY", 1) + _lifecycle("sell", "SELL", 1)
        for event in events:
            if event["status"] == "FILLED":
                event["evolution_provenance"] = {
                    "schema_version": 1,
                    "decision_manifest_sha256": "a" * 64,
                    "input_sha256": "b" * 64,
                    "pending_order_run_id": "anything",
                    "fill_run_id": event["run_id"],
                }
        replay = derive_completed_round_trips(events)
        self.assertEqual(replay["completed_round_trip_count"], 1)
        self.assertEqual(replay["eligible_sample_count"], 0)

    def test_current_legacy_prefix_is_git_anchored_and_replays_zero_round_trips(self):
        ledger = verify_event_ledger()
        replay = verify_portfolio_projection(ledger["_events"], DEFAULT_PORTFOLIO)
        self.assertEqual(ledger["anchor"]["status"], "VERIFIED_LEGACY_GIT_ANCHORED")
        self.assertEqual(ledger["legacy_event_count"], 4)
        self.assertEqual(
            ledger["event_count"],
            ledger["legacy_event_count"] + ledger["v2_event_count"],
        )
        self.assertEqual(replay["filled_event_count"], 2)
        self.assertEqual(replay["completed_round_trip_count"], 0)
        self.assertEqual(replay["positions"], {"510300": "1200", "512100": "800"})

    def test_legacy_prefix_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, anchor = _make_anchored_ledger(root)
            values = ledger.read_text(encoding="utf-8").splitlines()
            first = json.loads(values[0])
            first["quantity"] += 100
            values[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
            ledger.write_text("\n".join(values) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvolutionGateError, "canonical payload hash mismatch"):
                verify_event_ledger(ledger, anchor_path=anchor, repo_root=root)

    def test_production_anchor_cannot_verify_a_copied_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "orders.jsonl"
            shutil.copyfile(DEFAULT_LEDGER, ledger)
            with self.assertRaisesRegex(EvolutionGateError, "path does not match"):
                verify_event_ledger(ledger)

    def test_first_v2_event_extends_legacy_without_rewriting_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, anchor = _make_anchored_ledger(root)
            prefix = ledger.read_bytes()
            event = append_order_event(
                ledger,
                {
                    "order_id": "future-order",
                    "status": "PENDING_NEXT_OPEN",
                    "side": "BUY",
                    "symbol": "TEST",
                    "quantity": 1,
                },
                recorded_at="2026-07-21T10:00:00+08:00",
                anchor_path=anchor,
                repo_root=root,
            )
            self.assertTrue(ledger.read_bytes().startswith(prefix))
            self.assertEqual(event["_ledger"]["sequence"], 5)
            self.assertEqual(
                event["_ledger"]["previous_event_sha256"],
                "7c060e5b0ff0f7b9222a194e407cff592dcb7dfc08bef06bbe65379957a8f72f",
            )
            verified = verify_event_ledger(ledger, anchor_path=anchor, repo_root=root)
            self.assertEqual(verified["v2_event_count"], 1)

    def test_deterministic_event_id_is_idempotent_and_collision_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "orders.jsonl"
            event = _pending("same", "BUY", 1)
            first = append_order_event(
                ledger,
                event,
                event_id="deterministic-id",
                recorded_at="2026-07-21T10:00:00+08:00",
            )
            second = append_order_event(
                ledger,
                event,
                event_id="deterministic-id",
                recorded_at="2026-07-21T10:00:00+08:00",
            )
            self.assertEqual(first, second)
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)
            changed = dict(event)
            changed["quantity"] = 2
            with self.assertRaisesRegex(EvolutionGateError, "collision"):
                append_order_event(
                    ledger,
                    changed,
                    event_id="deterministic-id",
                    recorded_at="2026-07-21T10:00:00+08:00",
                )

    def test_committed_v2_suffix_cannot_be_recomputed_after_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger, anchor = _make_anchored_ledger(root)
            append_order_event(
                ledger,
                _pending("future-order", "BUY", 1),
                recorded_at="2026-07-21T10:00:00+08:00",
                anchor_path=anchor,
                repo_root=root,
            )
            subprocess.run(["git", "-C", str(root), "add", "data/ledger/orders.jsonl"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "commit v2"], check=True)
            values = ledger.read_text(encoding="utf-8").splitlines()
            rewritten = json.loads(values[-1])
            rewritten["quantity"] = 2
            rewritten["_ledger"]["event_sha256"] = event_sha256(rewritten)
            values[-1] = json.dumps(rewritten, ensure_ascii=False, sort_keys=True)
            ledger.write_text("\n".join(values) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvolutionGateError, "rewrites the Git HEAD"):
                verify_event_ledger(ledger, anchor_path=anchor, repo_root=root)

    def test_empty_v2_chain_detects_hash_and_sequence_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "orders.jsonl"
            first = append_order_event(
                ledger,
                _fill("buy-1", "BUY", 2),
                recorded_at="2026-07-21T10:00:00+08:00",
            )
            second = append_order_event(
                ledger,
                _fill("sell-1", "SELL", 2),
                recorded_at="2026-07-21T10:01:00+08:00",
            )
            self.assertEqual(first["_ledger"]["sequence"], 1)
            self.assertEqual(first["_ledger"]["previous_event_sha256"], GENESIS_EVENT_SHA256)
            self.assertEqual(second["_ledger"]["sequence"], 2)
            values = ledger.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(values[1])
            tampered["quantity"] = 1
            values[1] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
            ledger.write_text("\n".join(values) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvolutionGateError, "event hash mismatch"):
                verify_event_ledger(ledger)

    def test_round_trip_requires_exact_return_to_zero(self):
        events = _lifecycle("b1", "BUY", 10) + _lifecycle("b2", "BUY", 5) + _lifecycle("s1", "SELL", 4)
        partial = derive_completed_round_trips(events)
        self.assertEqual(partial["completed_round_trip_count"], 0)
        complete = derive_completed_round_trips(events + _lifecycle("s2", "SELL", 11))
        self.assertEqual(complete["completed_round_trip_count"], 1)
        self.assertEqual(complete["eligible_sample_count"], 0)
        self.assertEqual(complete["positions"], {})

    def test_duplicate_fill_and_oversell_fail_closed(self):
        with self.assertRaisesRegex(EvolutionGateError, "unique preceding pending"):
            derive_completed_round_trips(_lifecycle("same", "BUY", 1) + [_fill("same", "BUY", 1)])
        with self.assertRaisesRegex(EvolutionGateError, "oversells"):
            derive_completed_round_trips(_lifecycle("b", "BUY", 1) + _lifecycle("s", "SELL", 2))

    def test_direct_filled_events_cannot_fabricate_twenty_round_trips(self):
        events = []
        for index in range(20):
            events.extend([_fill(f"b{index}", "BUY", 1), _fill(f"s{index}", "SELL", 1)])
        with self.assertRaisesRegex(EvolutionGateError, "no unique preceding pending order"):
            derive_completed_round_trips(events)

    def test_portfolio_aggregate_cannot_inflate_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            portfolio = Path(directory) / "portfolio.json"
            portfolio.write_text(
                json.dumps(
                    {
                        "positions": {"TEST": {"quantity": 1}},
                        "performance": {"filled_trade_count": 999},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvolutionGateError, "filled_trade_count"):
                verify_portfolio_projection(_lifecycle("b", "BUY", 1), portfolio)


class EvolutionDecisionTests(unittest.TestCase):
    def _proposal(self, root: Path) -> Path:
        candidate = root / "candidate_strategy.json"
        shutil.copyfile(REPO_ROOT / "config/strategy.json", candidate)
        proposal = root / "proposal.json"
        proposal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "test-governance-hardening",
                    "hypothesis": "legacy acceptance must be recomputed by a protected verifier",
                    "candidate_strategy": {
                        "path": candidate.name,
                        "sha256": sha256_file(candidate),
                        "version": "0.3.0",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return proposal

    def test_current_candidate_is_proposed_only_and_cannot_be_self_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            result = verify_evolution_gate(
                self._proposal(Path(directory)),
                baseline_ref=BASELINE_REF,
            )
        self.assertEqual(result["decision"], "PROPOSED_ONLY")
        self.assertEqual(result["legacy_acceptance_status"], "INVALID_LEGACY_ACCEPTANCE")
        self.assertEqual(result["ledger"]["completed_round_trip_count"], 0)
        self.assertIn("ELIGIBLE_ROUND_TRIPS_0_LT_20", result["reasons"])
        self.assertIn("TRUSTED_EVALUATOR_UNAVAILABLE", result["reasons"])

    def test_caller_decision_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            proposal = self._proposal(Path(directory))
            with mock.patch.dict(os.environ, {"VIBE_EVOLUTION_DECISION": "ACCEPTED"}, clear=False):
                with self.assertRaisesRegex(EvolutionGateError, "caller-supplied"):
                    verify_evolution_gate(proposal, baseline_ref=BASELINE_REF)

    def test_old_or_semantics_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal = self._proposal(root)
            mode_lock = root / "MODE_LOCK.json"
            value = json.loads(DEFAULT_MODE_LOCK.read_text(encoding="utf-8"))
            value["evolution_policy"].pop("walk_forward_and_independent_oos_required")
            value["evolution_policy"]["walk_forward_or_out_of_sample_required"] = True
            mode_lock.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(EvolutionGateError, "walk-forward AND"):
                verify_evolution_gate(
                    proposal,
                    mode_lock_path=mode_lock,
                    baseline_ref=BASELINE_REF,
                )


if __name__ == "__main__":
    unittest.main()
