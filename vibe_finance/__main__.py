from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evolution import (
    DEFAULT_ANCHOR as DEFAULT_EVOLUTION_ANCHOR,
    DEFAULT_LEDGER as DEFAULT_EVENT_LEDGER,
    DEFAULT_MODE_LOCK,
    DEFAULT_PORTFOLIO,
    verify_evolution_gate,
    write_json_atomic,
)
from .evaluation import (
    DEFAULT_EVALUATION_MANIFEST,
    DEFAULT_SOURCES,
    DEFAULT_TRADING_CALENDAR,
    DEFAULT_UNIVERSE,
    audit_evaluation_readiness,
    verify_readiness_artifact,
    write_readiness_artifact,
)
from .frozen_baseline import (
    run_frozen_b0_mechanical,
    verify_frozen_b0_artifact,
    verify_frozen_b0_sources,
)
from .open_capture import (
    DEFAULT_UNIVERSE as DEFAULT_CAPTURE_UNIVERSE,
    capture_open_snapshot,
)

from .pipeline import (
    DEFAULT_LEDGER,
    DEFAULT_EXECUTION_REPORT_DIR,
    DEFAULT_FUND_REPORT_DIR,
    DEFAULT_ORDERS_LOG,
    DEFAULT_REPORT_DIR,
    DEFAULT_README,
    DEFAULT_STRATEGY,
    initialize_ledger,
    project_status,
    record_api_cost,
    run_fund_nav_pipeline,
    run_pipeline,
    settle_open_orders,
    update_readme_status,
    validate_snapshot_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe-finance",
        description="中国股票/基金虚拟组合的可审计分析闭环（不连接真实交易）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化 30,000 元项目账本")
    init.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    validate = sub.add_parser("validate", help="只校验点时输入，不产生决策")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)

    run = sub.add_parser("run", help="结算旧虚拟订单、分析并生成下一时点订单")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    run.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    run.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    run.add_argument("--orders-log", type=Path, default=DEFAULT_ORDERS_LOG)
    run.add_argument("--mode", choices=("short", "long", "preopen"), default="short")

    settle = sub.add_parser("settle-open", help="只结算上一收盘生成的开盘虚拟订单")
    settle.add_argument("--input", type=Path, required=True)
    settle.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    settle.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    settle.add_argument("--report-dir", type=Path, default=DEFAULT_EXECUTION_REPORT_DIR)
    settle.add_argument("--orders-log", type=Path, default=DEFAULT_ORDERS_LOG)

    capture = sub.add_parser(
        "capture-open",
        help="仅在09:30-09:35窗口内自动封存双源开盘快照",
    )
    capture.add_argument("--base-snapshot", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    capture.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    capture.add_argument("--universe", type=Path, default=DEFAULT_CAPTURE_UNIVERSE)

    funds = sub.add_parser("run-funds", help="按下一开放日确认净值处理场外基金")
    funds.add_argument("--input", type=Path, required=True)
    funds.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    funds.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    funds.add_argument("--report-dir", type=Path, default=DEFAULT_FUND_REPORT_DIR)
    funds.add_argument("--orders-log", type=Path, default=DEFAULT_ORDERS_LOG)

    status = sub.add_parser("status", help="只读检查心跳、报告和 API 预算")
    status.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    status.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    status.add_argument("--max-age-hours", type=float, default=36.0)

    cost = sub.add_parser("record-api-cost", help="记录一次实际 DeepSeek 调用成本")
    cost.add_argument("--amount-cny", type=float, required=True)
    cost.add_argument("--model", required=True)
    cost.add_argument("--purpose", required=True)
    cost.add_argument("--input-tokens", type=int, required=True)
    cost.add_argument("--output-tokens", type=int, required=True)
    cost.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    cost.add_argument("--cost-log", type=Path, default=Path("data/ledger/api_costs.jsonl"))

    readme = sub.add_parser("update-readme", help="从虚拟账本刷新 README 的公开状态区块")
    readme.add_argument("--readme", type=Path, default=DEFAULT_README)
    readme.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    evolution = sub.add_parser("evolution-gate", help="只读计算策略进化门禁，调用方不能指定结论")
    evolution.add_argument("--proposal", type=Path, required=True)
    evolution.add_argument("--ledger", type=Path, default=DEFAULT_EVENT_LEDGER)
    evolution.add_argument("--portfolio", type=Path, default=DEFAULT_PORTFOLIO)
    evolution.add_argument("--anchor", type=Path, default=DEFAULT_EVOLUTION_ANCHOR)
    evolution.add_argument("--mode-lock", type=Path, default=DEFAULT_MODE_LOCK)
    evolution.add_argument("--baseline-ref", required=True)
    evolution.add_argument("--output", type=Path)

    readiness = sub.add_parser(
        "evaluation-readiness",
        help="只读审计点时历史是否足以运行 B0/B1/B2 走前与独立 OOS",
    )
    readiness.add_argument("--inputs", type=Path, nargs="+", required=True)
    readiness.add_argument(
        "--minimum-unique-dates",
        type=int,
        help="只能收紧 manifest 的共同日期下限，不能放宽",
    )
    readiness.add_argument("--output", type=Path)

    verify_readiness = sub.add_parser(
        "evaluation-verify-readiness",
        help="重载 readiness 工件并重新核验所有输入绑定哈希",
    )
    verify_readiness.add_argument("--artifact", type=Path, required=True)

    sub.add_parser(
        "evaluation-b0-source-check",
        help="只读核验冻结 B0 的历史 commit、策略和 pipeline 哈希（不运行绩效）",
    )
    b0_smoke = sub.add_parser(
        "evaluation-b0-mechanical",
        help="隔离重放冻结 B0 历史 fixture 并保存不可覆盖工件（不运行绩效）",
    )
    b0_smoke.add_argument("--inputs", type=Path, nargs="+", required=True)
    b0_smoke.add_argument("--output", type=Path, required=True)

    b0_verify = sub.add_parser(
        "evaluation-b0-verify",
        help="重载冻结 B0 历史 fixture 烟测工件并重新执行核验",
    )
    b0_verify.add_argument("--artifact", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        result = initialize_ledger(args.ledger)
    elif args.command == "validate":
        result = validate_snapshot_file(args.input, args.strategy)
    elif args.command == "run":
        result = run_pipeline(
            input_path=args.input,
            ledger_path=args.ledger,
            strategy_path=args.strategy,
            report_dir=args.report_dir,
            orders_log=args.orders_log,
            mode=args.mode,
        )
    elif args.command == "settle-open":
        result = settle_open_orders(
            input_path=args.input,
            ledger_path=args.ledger,
            strategy_path=args.strategy,
            report_dir=args.report_dir,
            orders_log=args.orders_log,
        )
    elif args.command == "capture-open":
        result = capture_open_snapshot(
            base_snapshot_path=args.base_snapshot,
            output_path=args.output,
            ledger_path=args.ledger,
            strategy_path=args.strategy,
            universe_path=args.universe,
        )
    elif args.command == "run-funds":
        result = run_fund_nav_pipeline(
            input_path=args.input,
            ledger_path=args.ledger,
            strategy_path=args.strategy,
            report_dir=args.report_dir,
            orders_log=args.orders_log,
        )
    elif args.command == "status":
        result = project_status(
            ledger_path=args.ledger,
            report_dir=args.report_dir,
            max_age_hours=args.max_age_hours,
        )
    elif args.command == "record-api-cost":
        result = record_api_cost(
            ledger_path=args.ledger,
            cost_log=args.cost_log,
            amount_cny=args.amount_cny,
            model=args.model,
            purpose=args.purpose,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
        )
    elif args.command == "evolution-gate":
        result = verify_evolution_gate(
            proposal_path=args.proposal,
            ledger_path=args.ledger,
            portfolio_path=args.portfolio,
            anchor_path=args.anchor,
            mode_lock_path=args.mode_lock,
            baseline_ref=args.baseline_ref,
        )
        if args.output:
            write_json_atomic(args.output, result)
    elif args.command == "evaluation-readiness":
        result = audit_evaluation_readiness(
            args.inputs,
            sources_path=DEFAULT_SOURCES,
            universe_path=DEFAULT_UNIVERSE,
            manifest_path=DEFAULT_EVALUATION_MANIFEST,
            calendar_path=DEFAULT_TRADING_CALENDAR,
            minimum_unique_dates=args.minimum_unique_dates,
        )
        if args.output:
            write_readiness_artifact(args.output, result)
    elif args.command == "evaluation-verify-readiness":
        result = verify_readiness_artifact(args.artifact)
    elif args.command == "evaluation-b0-source-check":
        result = verify_frozen_b0_sources(
            Path(__file__).resolve().parents[1], DEFAULT_EVALUATION_MANIFEST
        )
    elif args.command == "evaluation-b0-mechanical":
        result = run_frozen_b0_mechanical(
            Path(__file__).resolve().parents[1],
            DEFAULT_EVALUATION_MANIFEST,
            args.inputs,
            output_path=args.output,
        )
    elif args.command == "evaluation-b0-verify":
        result = verify_frozen_b0_artifact(
            Path(__file__).resolve().parents[1],
            DEFAULT_EVALUATION_MANIFEST,
            args.artifact,
        )
    else:
        result = update_readme_status(
            readme_path=args.readme,
            ledger_path=args.ledger,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if args.command == "evaluation-readiness" and result["status"] != "READY_FOR_MECHANICAL_EVALUATION":
        raise SystemExit(2)
    if args.command == "evaluation-verify-readiness":
        if result["status"] == "VERIFIED_NOT_EVALUABLE":
            raise SystemExit(2)
        if result["status"] != "VERIFIED_READY":
            raise SystemExit(3)
    if args.command == "evaluation-b0-verify" and result["status"] == "INVALID":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
