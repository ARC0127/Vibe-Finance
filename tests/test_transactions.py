from __future__ import annotations

import json
import os
import tempfile
import unittest
import hashlib
from pathlib import Path

from tests.test_pipeline import STRATEGY_PATH, snapshot
from vibe_finance.evolution import (
    EvolutionGateError,
    append_order_event,
    verify_event_ledger,
    verify_portfolio_projection,
)
from vibe_finance.pipeline import (
    initialize_ledger,
    project_status,
    record_api_cost,
    run_pipeline,
)
from vibe_finance.transaction import (
    TransactionError,
    _runtime_path,
    inspect_transaction_state,
    locked_state,
    prepare_run_transaction,
    recover_incomplete_transactions,
)


class TransactionTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_runtime_path_maps_windows_and_wsl_mounts(self) -> None:
        if os.name == "nt":
            self.assertEqual(
                _runtime_path("/mnt/f/Git/Vibe Finance/report.json"),
                Path("F:/Git/Vibe Finance/report.json"),
            )
        else:
            self.assertEqual(
                _runtime_path(r"\\?\F:\Git\Vibe Finance\report.json"),
                Path("/mnt/f/Git/Vibe Finance/report.json"),
            )

    def test_failed_verification_does_not_leave_orphan_transaction_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger)
            orders.write_text("not-json\n", encoding="utf-8")

            with self.assertRaises(EvolutionGateError):
                prepare_run_transaction(
                    run_id="must-not-be-created",
                    ledger_path=ledger,
                    orders_log=orders,
                    recorded_at="2026-07-22T20:00:00+08:00",
                    portfolio=json.loads(ledger.read_text(encoding="utf-8")),
                    decision_path=root / "decision.json",
                    decision={"run_id": "must-not-be-created"},
                    report_path=root / "report.md",
                    report_text="unused\n",
                    heartbeat={"run_id": "must-not-be-created"},
                    events=[],
                )

            self.assertFalse((root / "transactions" / "must-not-be-created").exists())

    def test_orphan_transaction_directory_is_not_reported_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            initialize_ledger(ledger)
            (root / "transactions" / "orphan").mkdir(parents=True)

            status = inspect_transaction_state(ledger)

            self.assertEqual(status["reason"], "ORPHAN_TRANSACTION_DIRECTORY")
            self.assertFalse(status["recoverable"])

    def test_cash_projection_rejects_internally_inconsistent_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "portfolio.json"
            initialize_ledger(ledger)
            value = json.loads(ledger.read_text(encoding="utf-8"))
            value["cash_cny"] = 1.0
            ledger.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(EvolutionGateError, "portfolio cash"):
                verify_portfolio_projection([], ledger)

    def test_recovery_rejects_matching_event_id_with_different_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger_path)
            portfolio = json.loads(ledger_path.read_text(encoding="utf-8"))
            event = {
                "run_id": "payload-run",
                "order_id": "pending-payload",
                "status": "PENDING_NEXT_OPEN",
                "side": "BUY",
                "symbol": "TEST",
                "quantity": 100,
                "simulation_only": True,
                "signal_as_of": "2026-07-17T15:00:00+08:00",
            }
            portfolio["pending_orders"] = [dict(event)]
            portfolio["last_run_id"] = "payload-run"

            def fail_after_prepare(stage: str) -> None:
                if stage == "after_prepare":
                    raise RuntimeError("fault injection")

            with locked_state(ledger_path, exclusive=True):
                with self.assertRaises(RuntimeError):
                    prepare_run_transaction(
                        run_id="payload-run",
                        ledger_path=ledger_path,
                        orders_log=orders,
                        recorded_at="2026-07-17T15:00:00+08:00",
                        portfolio=portfolio,
                        decision_path=root / "decision.json",
                        decision={"schema_version": 1, "run_id": "payload-run"},
                        report_path=root / "report.md",
                        report_text="payload\n",
                        heartbeat={"run_id": "payload-run"},
                        events=[event],
                        fault_hook=fail_after_prepare,
                    )
                prepare = json.loads(
                    (
                        root
                        / "transactions"
                        / "payload-run"
                        / "prepare.json"
                    ).read_text(encoding="utf-8")
                )
                changed = dict(event)
                changed["quantity"] = 200
                append_order_event(
                    orders,
                    changed,
                    event_id=prepare["events"][0]["event_id"],
                    recorded_at="2026-07-17T15:00:00+08:00",
                )
                with self.assertRaisesRegex(TransactionError, "payload differs"):
                    recover_incomplete_transactions(ledger_path)

    def test_api_cost_is_a_state_transaction_and_preserves_market_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(snapshot("2026-07-17", trading=False)), encoding="utf-8"
            )
            ledger = root / "portfolio.json"
            orders = root / "orders.jsonl"
            run_pipeline(
                input_path=input_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=orders,
            )
            before = json.loads(ledger.read_text(encoding="utf-8"))
            market_as_of = before["performance"]["last_valuation_as_of"]
            result = record_api_cost(
                ledger_path=ledger,
                cost_log=root / "api_costs.jsonl",
                amount_cny=0.01,
                model="test-model",
                purpose="transaction regression",
                input_tokens=1,
                output_tokens=1,
            )
            after = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "RECORDED")
            self.assertEqual(after["performance"]["last_valuation_as_of"], market_as_of)
            self.assertEqual(after["research_infrastructure"]["actual_calls"], 1)
            self.assertEqual(
                len((root / "api_costs.jsonl").read_text(encoding="utf-8").splitlines()),
                1,
            )
            self.assertEqual(inspect_transaction_state(ledger)["status"], "COMMITTED")
            self.assertEqual(project_status(ledger, root / "reports")["status"], "ACTIVE")

    def test_hash_bound_zero_event_invalidation_restores_last_valid_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger)
            baseline_portfolio = json.loads(ledger.read_text(encoding="utf-8"))
            baseline_portfolio["last_run_id"] = "valid-run"
            baseline_portfolio["run_history"].append(
                {"run_id": "valid-run", "run_date": "2026-08-19", "mode": "fund_nav", "input_sha256": "1" * 64}
            )
            valid_heartbeat = {
                "status": "ACTIVE",
                "last_success_at": "2026-08-19T15:54:05+00:00",
                "run_id": "valid-run",
                "run_date": "2026-08-19",
                "mode": "fund_nav",
                "input_sha256": "1" * 64,
            }
            with locked_state(ledger, exclusive=True):
                prepare_run_transaction(
                    run_id="valid-run",
                    ledger_path=ledger,
                    orders_log=orders,
                    recorded_at="2026-08-19T15:54:05+00:00",
                    portfolio=baseline_portfolio,
                    decision_path=root / "valid.json",
                    decision={"schema_version": 1, "run_id": "valid-run", "mode": "fund_nav"},
                    report_path=root / "valid.md",
                    report_text="valid\n",
                    heartbeat=valid_heartbeat,
                    events=[],
                )
            valid_portfolio_bytes = ledger.read_bytes()
            valid_heartbeat_bytes = (root / "heartbeat.json").read_bytes()
            invalid_portfolio = json.loads(valid_portfolio_bytes)
            invalid_portfolio["last_run_id"] = "invalid-run"
            invalid_portfolio["run_history"].append(
                {"run_id": "invalid-run", "run_date": "2026-08-20", "mode": "preopen", "input_sha256": "2" * 64}
            )
            with locked_state(ledger, exclusive=True):
                prepare_run_transaction(
                    run_id="invalid-run",
                    ledger_path=ledger,
                    orders_log=orders,
                    recorded_at="2026-08-20T14:26:18+00:00",
                    portfolio=invalid_portfolio,
                    decision_path=root / "invalid.json",
                    decision={"schema_version": 1, "run_id": "invalid-run", "mode": "preopen"},
                    report_path=root / "invalid.md",
                    report_text="invalid\n",
                    heartbeat={**valid_heartbeat, "run_id": "invalid-run", "run_date": "2026-08-20", "mode": "preopen"},
                    events=[],
                )
            ledger.write_bytes(valid_portfolio_bytes)
            (root / "heartbeat.json").write_bytes(valid_heartbeat_bytes)
            self.assertEqual(inspect_transaction_state(ledger)["status"], "INCOMPLETE")

            evidence = root / "invalidation-evidence.json"
            evidence.write_text('{"status":"FAILED_TIME_WINDOW_CLOSED"}\n', encoding="utf-8")
            run_dir = root / "transactions" / "invalid-run"
            record = {
                "schema_version": 1,
                "run_id": "invalid-run",
                "reason": "INVALID_TEMPORAL_PROVENANCE",
                "prepare_sha256": self._sha256(run_dir / "prepare.json"),
                "commit_sha256": self._sha256(run_dir / "commit.json"),
                "evidence_path": str(evidence),
                "evidence_sha256": self._sha256(evidence),
            }
            (root / "transaction_invalidations.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )
            state = inspect_transaction_state(ledger)
            self.assertEqual(state["status"], "COMMITTED")
            self.assertEqual(state["latest_commit"]["run_id"], "valid-run")
            self.assertEqual(state["invalidated_run_ids"], ["invalid-run"])
            self.assertEqual(project_status(ledger, root / "reports")["last_run_id"], "valid-run")

    def test_transaction_invalidation_cannot_suppress_financial_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger)
            portfolio = json.loads(ledger.read_text(encoding="utf-8"))
            portfolio["last_run_id"] = "event-run"
            event = {
                "run_id": "event-run",
                "order_id": "event-order",
                "status": "PENDING_NEXT_OPEN",
                "side": "BUY",
                "symbol": "TEST",
                "quantity": 100,
                "simulation_only": True,
                "signal_as_of": "2026-08-20T09:09:59+08:00",
            }
            portfolio["pending_orders"] = [dict(event)]
            portfolio["run_history"].append(
                {"run_id": "event-run", "run_date": "2026-08-20", "mode": "preopen", "input_sha256": "3" * 64}
            )
            with locked_state(ledger, exclusive=True):
                prepare_run_transaction(
                    run_id="event-run",
                    ledger_path=ledger,
                    orders_log=orders,
                    recorded_at="2026-08-20T14:26:18+00:00",
                    portfolio=portfolio,
                    decision_path=root / "event.json",
                    decision={"schema_version": 1, "run_id": "event-run", "mode": "preopen"},
                    report_path=root / "event.md",
                    report_text="event\n",
                    heartbeat={"run_id": "event-run"},
                    events=[event],
                )
            evidence = root / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            run_dir = root / "transactions" / "event-run"
            (root / "transaction_invalidations.jsonl").write_text(
                json.dumps({
                    "schema_version": 1,
                    "run_id": "event-run",
                    "reason": "INVALID_TEMPORAL_PROVENANCE",
                    "prepare_sha256": self._sha256(run_dir / "prepare.json"),
                    "commit_sha256": self._sha256(run_dir / "commit.json"),
                    "evidence_path": str(evidence),
                    "evidence_sha256": self._sha256(evidence),
                }) + "\n",
                encoding="utf-8",
            )
            state = inspect_transaction_state(ledger)
            self.assertEqual(state["status"], "INCOMPLETE")
            self.assertIn("cannot suppress financial events", state["reason"])

    def test_pipeline_commits_event_portfolio_reports_and_heartbeat_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            input_path.write_text(
                json.dumps(snapshot("2026-07-17", trading=True)), encoding="utf-8"
            )
            ledger = root / "portfolio.json"
            orders = root / "orders.jsonl"
            result = run_pipeline(
                input_path=input_path,
                ledger_path=ledger,
                strategy_path=STRATEGY_PATH,
                report_dir=root / "reports",
                orders_log=orders,
            )
            self.assertIn(result["status"], {"PASS", "FAILED_DAILY_ORDER"})
            state = inspect_transaction_state(ledger)
            self.assertEqual(state["status"], "COMMITTED")
            self.assertEqual(state["latest_commit"]["run_id"], result["run_id"])
            self.assertEqual(project_status(ledger, root / "reports")["status"], "ACTIVE")
            verified = verify_event_ledger(orders)
            self.assertEqual(
                verify_portfolio_projection(verified["_events"], ledger)["status"],
                "PASS",
            )

    def test_after_events_failure_is_visible_and_recovery_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger_path)
            portfolio = json.loads(ledger_path.read_text(encoding="utf-8"))
            run_id = "recoverable-run"
            event = {
                "run_id": run_id,
                "order_id": "pending-1",
                "status": "PENDING_NEXT_OPEN",
                "side": "BUY",
                "symbol": "TEST",
                "quantity": 100,
                "simulation_only": True,
                "signal_as_of": "2026-07-17T15:00:00+08:00",
            }
            portfolio["pending_orders"] = [dict(event)]
            portfolio["last_run_id"] = run_id
            portfolio["run_history"].append(
                {
                    "run_id": run_id,
                    "run_date": "2026-07-17",
                    "mode": "short",
                    "input_sha256": "0" * 64,
                }
            )

            def fail_after_events(stage: str) -> None:
                if stage == "after_events":
                    raise RuntimeError("fault injection")

            with locked_state(ledger_path, exclusive=True):
                with self.assertRaisesRegex(RuntimeError, "fault injection"):
                    prepare_run_transaction(
                        run_id=run_id,
                        ledger_path=ledger_path,
                        orders_log=orders,
                        recorded_at="2026-07-17T15:00:00+08:00",
                        portfolio=portfolio,
                        decision_path=root / "reports" / "decision.json",
                        decision={
                            "schema_version": 1,
                            "run_id": run_id,
                            "mode": "short",
                            "new_orders": [
                                {key: value for key, value in event.items() if key != "run_id"}
                            ],
                        },
                        report_path=root / "reports" / "report.md",
                        report_text="recovery report\n",
                        heartbeat={
                            "status": "ACTIVE",
                            "last_success_at": "2026-07-17T15:01:00+08:00",
                            "run_id": run_id,
                            "run_date": "2026-07-17",
                            "mode": "short",
                            "input_sha256": "0" * 64,
                        },
                        events=[event],
                        fault_hook=fail_after_events,
                    )

            before = orders.read_bytes()
            status = project_status(ledger_path, root / "reports")
            self.assertEqual(status["status"], "INCOMPLETE")
            self.assertEqual(status["reason"], "PREPARED_NOT_COMMITTED")
            with locked_state(ledger_path, exclusive=True):
                recovered = recover_incomplete_transactions(ledger_path)
                second = recover_incomplete_transactions(ledger_path)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(second, [])
            self.assertEqual(orders.read_bytes(), before)
            self.assertEqual(len(orders.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(inspect_transaction_state(ledger_path)["status"], "COMMITTED")
            self.assertEqual(project_status(ledger_path, root / "reports")["status"], "STALE")

    def test_recovery_does_not_overwrite_conflicting_immutable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "portfolio.json"
            orders = root / "orders.jsonl"
            initialize_ledger(ledger_path)
            portfolio = json.loads(ledger_path.read_text(encoding="utf-8"))

            def fail_after_portfolio(stage: str) -> None:
                if stage == "after_portfolio":
                    raise RuntimeError("fault injection")

            with locked_state(ledger_path, exclusive=True):
                with self.assertRaises(RuntimeError):
                    prepare_run_transaction(
                        run_id="report-conflict",
                        ledger_path=ledger_path,
                        orders_log=orders,
                        recorded_at="2026-07-17T15:00:00+08:00",
                        portfolio=portfolio,
                        decision_path=root / "decision.json",
                        decision={"schema_version": 1, "run_id": "report-conflict"},
                        report_path=root / "report.md",
                        report_text="expected\n",
                        heartbeat={"run_id": "report-conflict"},
                        events=[],
                        fault_hook=fail_after_portfolio,
                    )
            (root / "decision.json").write_text("foreign", encoding="utf-8")
            with locked_state(ledger_path, exclusive=True):
                with self.assertRaisesRegex(TransactionError, "immutable artifact conflict"):
                    recover_incomplete_transactions(ledger_path)
            self.assertEqual((root / "decision.json").read_text(encoding="utf-8"), "foreign")


if __name__ == "__main__":
    unittest.main()
