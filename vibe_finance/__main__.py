from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    else:
        result = update_readme_status(
            readme_path=args.readme,
            ledger_path=args.ledger,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
