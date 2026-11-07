"""从 IB Gateway 账户同步真实持仓到 portfolio.json: python -m tracker.ibkr_sync

兼容入口 — 导入功能已统一到 tracker.importer (CLI: tracker import ibkr);
本模块保留 python -m tracker.ibkr_sync 与 `tracker sync` 调用路径。
缺省追加合并 (与 tracker import ibkr / import file 一致); --overwrite 才覆盖全部。
"""

from __future__ import annotations

import argparse

from . import importer
from .cli._common import _finish_with_error
from .cli._import_report import run_import_report
from .storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO


def run_sync(args) -> None:
    try:
        rows, skipped = importer.collect_ibkr(mode=getattr(args, "mode", None))
    except Exception as e:
        _finish_with_error(f"{e} (请确认 IB Gateway 已登录运行, 且 API 连接已启用)")

    run_import_report(
        rows,
        skipped,
        args,
        title="IBKR 账户持仓",
        empty_msg="(无股票持仓或均无法映射)",
        tip=(
            "提示: avg_cost 为 IBKR 报告的合约货币每股均价 (含佣金); "
            "英股 (LSE 主板) IBKR 以便士报价, 已自动 ÷100 折成英镑。仅供估算。"
        ),
    )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="IBKR 账户持仓同步 (等价 tracker import ibkr)"
    )
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--dry-run", action="store_true", help="仅打印, 不写入")
    ap.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置",
    )
    ap.add_argument(
        "--append",
        action="store_true",
        help="追加合并 (按代码更新/新增) — 缺省行为, 保留兼容旧调用",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖全部持仓 (清空后重写)",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_sync(args)


if __name__ == "__main__":
    main()
