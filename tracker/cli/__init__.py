"""统一命令行入口: python -m tracker.cli <子命令> [选项]

子命令:
  snapshot  组合 + 自选快照 (持仓 / 自选提醒 / 阈值触发)
  quote     查询单个或多个代码实时行情
  watchlist 自选股管理 (list / add / remove) 与阈值提醒
  portfolio 持仓管理 (list / add / remove / set-base)
  report    导出自选监控阈值报告 (md / csv / json)
  export    导出投资组合快照为 CSV / JSON / Markdown 报表
  fx        汇率查询
  history   历史价格 (近 N 个月)
  kline     K线蜡烛图 (交互式 HTML + 摘要, 含成交量/均线/周月K)
  import    统一持仓导入 (ibkr / wallet / file, 追加合并或 --overwrite 覆盖)
  sync      从 IB Gateway 账户同步持仓 (已并入 import ibkr, 保留兼容)
  import-wallet 从链上地址导入加密资产 (已并入 import wallet, 保留兼容)
  cache     行情磁盘缓存管理 (info / clear)

所有子命令均支持 --json 输出机器可读结果, 便于脚本与 AI 消费。
"""
from __future__ import annotations

import argparse

# 暴露 prices 供测试 monkeypatch (cli.prices.get_quotes)
from .. import prices  # noqa: F401
from ..storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO
from ..watchlist import DEFAULT_WATCHLIST
from ._common import VERSION
from .cache import cmd_cache
from .export import cmd_export
from .fx import cmd_fx
from .history import cmd_history
from .kline import cmd_kline
from .portfolio import cmd_portfolio
from .quote import cmd_quote
from .report import cmd_report
from .snapshot import cmd_snapshot
from .sync import cmd_sync
from .importer import cmd_import
from .wallet import cmd_import_wallet
from .watchlist import cmd_watchlist


def build_parser() -> argparse.ArgumentParser:
    """构建顶层 argparse 解析器, 注册全部子命令."""
    ap = argparse.ArgumentParser(
        prog="tracker",
        description="OpenBB Portfolio Tracker 命令行工具 (持仓 / 自选 / 行情 / 汇率 / 缓存)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m tracker.cli snapshot --json\n"
            "  python -m tracker.cli quote AAPL 600519.SS\n"
            "  python -m tracker.cli watchlist add AAPL --upper1 250\n"
            "  python -m tracker.cli portfolio add AAPL --quantity 10 --avg-cost 180\n"
            "  python -m tracker.cli export -f md -o snapshot.md\n"
            "  python -m tracker.cli report -f csv -o report.csv\n"
        ),
    )
    ap.add_argument("-v", "--version", action="version", version=f"tracker {VERSION}")
    sub = ap.add_subparsers(dest="command", metavar="<子命令>", required=True)

    # ---- snapshot ----
    p_snap = sub.add_parser(
        "snapshot", help="组合 + 自选快照",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker snapshot                      # 查看全部持仓 + 自选\n"
            "  tracker snapshot --base USD           # 临时以 USD 为基础货币\n"
            "  tracker snapshot --watchlist 科技 --json  # 仅看「科技」列表并输出 JSON\n"
        ),
    )
    p_snap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_snap.add_argument("--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_snap.add_argument("--watchlist", default=None, help="只查看某个子自选列表 (默认全部)")
    p_snap.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    p_snap.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    p_snap.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)")
    p_snap.add_argument("--json", action="store_true", help="输出 JSON")
    p_snap.set_defaults(func=cmd_snapshot)

    # ---- quote ----
    p_q = sub.add_parser(
        "quote", help="查询实时行情",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker quote AAPL 600519.SS          # 查询多个代码\n"
            "  tracker quote 0700.HK --akshare       # 港股优先 akshare\n"
            "  tracker quote NVDA --json             # JSON 输出\n"
        ),
    )
    p_q.add_argument("symbols", nargs="+", help="Yahoo 代码, 如 AAPL 600519.SS BTC-USD")
    p_q.add_argument("--akshare", action="store_true")
    p_q.add_argument("--ibkr", action="store_true")
    p_q.add_argument("--json", action="store_true")
    p_q.set_defaults(func=cmd_quote)

    # ---- watchlist ----
    p_w = sub.add_parser(
        "watchlist", help="自选股管理 (list/add/remove) 与阈值提醒",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker watchlist list                          # 查看全部自选 + 行情\n"
            "  tracker watchlist list --no-quotes             # 仅展示配置\n"
            "  tracker watchlist add AAPL --list 科技 --upper1 250 --note 苹果\n"
            "  tracker watchlist add 600519.SS --list 白酒 --lower1 1500\n"
            "  tracker watchlist remove AAPL --list 科技       # 仅从「科技」列表移除\n"
        ),
    )
    p_w.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    p_w.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_w.add_argument("--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_w.add_argument("--list", dest="wl_list", default=None, help="所属列表名 (add 时指定 / list 时过滤)")
    p_w.add_argument("--upper1", type=float, help="上限 I")
    p_w.add_argument("--upper2", type=float, help="上限 II")
    p_w.add_argument("--lower1", type=float, help="下限 I")
    p_w.add_argument("--lower2", type=float, help="下限 II")
    p_w.add_argument("--note", help="备注")
    p_w.add_argument("--akshare", action="store_true")
    p_w.add_argument("--ibkr", action="store_true")
    p_w.add_argument("--json", action="store_true", help="输出 JSON")
    p_w.add_argument("--no-quotes", action="store_true", help="list 时不拉行情, 仅展示配置")
    p_w.set_defaults(func=cmd_watchlist)

    # ---- portfolio ----
    p_p = sub.add_parser(
        "portfolio", help="持仓管理 (list/add/remove/set-base)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker portfolio list                            # 列出持仓\n"
            "  tracker portfolio add AAPL --quantity 10 --avg-cost 180\n"
            "  tracker portfolio add 0700.HK --quantity 100 --avg-cost 330\n"
            "  tracker portfolio remove AAPL                     # 删除持仓\n"
            "  tracker portfolio set-base USD                     # 设置基础货币\n"
        ),
    )
    p_p.add_argument("action", nargs="?", choices=["list", "add", "remove", "set-base"],
                     default="list")
    p_p.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_p.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_p.add_argument("--quantity", type=float, help="持仓数量 (add 时必填)")
    p_p.add_argument("--avg-cost", type=float, help="成本价 (当地货币, add 时可选)")
    p_p.add_argument("--currency", help="基础货币 (set-base 时指定, 如 CNY/USD)")
    p_p.add_argument("--json", action="store_true", help="输出 JSON")
    p_p.set_defaults(func=cmd_portfolio)

    # ---- report ----
    p_r = sub.add_parser(
        "report", help="导出自选监控阈值报告 (md/csv/json)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker report -f md                    # 终端打印 Markdown 报告\n"
            "  tracker report -f csv -o report.csv     # 导出 CSV 文件\n"
            "  tracker report -f json -w 科技           # 仅「科技」列表, JSON 输出\n"
            "  tracker report -f md --sort severity     # 按严重度排序\n"
        ),
    )
    p_r.add_argument("--format", "-f", choices=["md", "csv", "json"], default="md",
                     help="输出格式 (默认 md)")
    p_r.add_argument("--watchlist", "-w", default=None, help="只导出某个子自选列表 (默认全部)")
    p_r.add_argument("--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_r.add_argument("--sort", choices=["default", "severity", "change_desc", "change_asc"],
                     default="default", help="列表内排序方式")
    p_r.add_argument("--akshare", action="store_true")
    p_r.add_argument("--ibkr", action="store_true")
    p_r.add_argument("--output", "-o", default=None, help="输出文件路径 (缺省打印到终端)")
    p_r.set_defaults(func=cmd_report)

    # ---- export ----
    p_e = sub.add_parser(
        "export", help="导出投资组合快照为 CSV/JSON/Markdown 报表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker export -f json -o snapshot.json   # 导出完整快照 JSON\n"
            "  tracker export -f csv -o holdings.csv      # 导出持仓 CSV\n"
            "  tracker export -f md -o snapshot.md         # 导出 Markdown 报表\n"
            "  tracker export -f md --base USD            # 临时以 USD 为基础货币\n"
        ),
    )
    p_e.add_argument("--format", "-f", choices=["csv", "json", "md"], default="json",
                     help="输出格式 (默认 json)")
    p_e.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_e.add_argument("--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_e.add_argument("--watchlist", "-w", default=None, help="只导出某个子自选列表 (默认全部)")
    p_e.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    p_e.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    p_e.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)")
    p_e.add_argument("--output", "-o", default=None, help="输出文件路径 (缺省打印到终端)")
    p_e.set_defaults(func=cmd_export)

    # ---- fx ----
    p_fx = sub.add_parser(
        "fx", help="汇率查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker fx USD                       # 1 USD 兑常用币种\n"
            "  tracker fx USD CNY HKD --json        # 指定币种 + JSON 输出\n"
            "  tracker fx CNY --ibkr                # 使用 IBKR 汇率\n"
        ),
    )
    p_fx.add_argument("base", help="基础货币, 如 USD")
    p_fx.add_argument("currencies", nargs="*", help="目标货币, 缺省常用币种")
    p_fx.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 汇率")
    p_fx.add_argument("--json", action="store_true")
    p_fx.set_defaults(func=cmd_fx)

    # ---- history ----
    p_h = sub.add_parser(
        "history", help="历史价格 (近 N 个月)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker history AAPL                  # 近 12 个月日线\n"
            "  tracker history 600519.SS --months 24  # 近 24 个月\n"
            "  tracker history AAPL --json            # JSON 输出\n"
        ),
    )
    p_h.add_argument("symbol")
    p_h.add_argument("--months", type=int, default=12)
    p_h.add_argument("--rows", type=int, default=10, help="表格模式打印最近 N 行")
    p_h.add_argument("--akshare", action="store_true")
    p_h.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)")
    p_h.add_argument("--json", action="store_true")
    p_h.set_defaults(func=cmd_history)

    # ---- kline ----
    p_k = sub.add_parser(
        "kline", help="K线蜡烛图 (生成交互式 HTML, 含成交量/均线)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker kline AAPL                              # 日K + 成交量 + MA5/20/60\n"
            "  tracker kline 600519.SS --period weekly         # 周K\n"
            "  tracker kline AAPL --ma 10,30 --refresh          # 自定义均线 + 强制刷新\n"
            "  tracker kline AAPL --no-volume --open            # 无成交量 + 浏览器打开\n"
        ),
    )
    p_k.add_argument("symbol", help="Yahoo 代码, 如 AAPL 600519.SS BTC-USD")
    p_k.add_argument("--months", type=int, default=12, help="拉取近 N 个月日线")
    p_k.add_argument("--period", choices=["daily", "weekly", "monthly"], default="daily",
                     help="K线周期 (默认日K)")
    p_k.add_argument("--ma", default="5,20,60", help="均线周期, 逗号分隔 (如 5,10,20,60)")
    p_k.add_argument("--no-volume", action="store_true", help="隐藏成交量副图")
    p_k.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    p_k.add_argument("--akshare", action="store_true")
    p_k.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)")
    p_k.add_argument("--output", "-o", default=None,
                     help="HTML 输出路径 (默认 data/kline_<代码>.html)")
    p_k.add_argument("--open", dest="open_browser", action="store_true",
                     help="生成后自动在浏览器打开")
    p_k.add_argument("--json", action="store_true", help="输出 JSON 数据 (不生成图表)")
    p_k.set_defaults(func=cmd_kline)

    # ---- import ----
    p_imp = sub.add_parser(
        "import", help="统一持仓导入 (ibkr / wallet / file)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "导入方式: 默认追加合并 (按代码更新数量/成本, 新代码追加, 其余持仓保留);\n"
            "          --overwrite 覆盖全部持仓 (写前自动备份 .bak)。\n"
            "示例:\n"
            "  tracker import ibkr --dry-run                     # 预览 IBKR 账户持仓\n"
            "  tracker import ibkr --mode live --overwrite       # 实盘账户, 覆盖写入\n"
            "  tracker import wallet eth 0xd8dA... --dry-run     # 预览链上余额\n"
            "  tracker import wallet bsc 0x... --base-currency USDT\n"
            "  tracker import file 持仓.csv                       # 券商导出文件追加导入\n"
            "  tracker import file 持仓.xlsx --overwrite --json\n"
        ),
    )
    imp_sub = p_imp.add_subparsers(dest="source", metavar="<来源>", required=True)

    p_i_ibkr = imp_sub.add_parser(
        "ibkr", help="从 IB Gateway 账户导入持仓",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker import ibkr --dry-run          # 仅预览\n"
            "  tracker import ibkr --mode paper       # 模拟账户 (端口 4002)\n"
            "  tracker import ibkr --overwrite        # 覆盖全部持仓\n"
        ),
    )
    p_i_ibkr.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_i_ibkr.add_argument("--mode", choices=["paper", "live"], default=None,
                          help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置")
    p_i_ibkr.add_argument("--overwrite", action="store_true",
                          help="覆盖全部持仓 (缺省追加合并)")
    p_i_ibkr.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_i_ibkr.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_ibkr.set_defaults(func=cmd_import)

    p_i_wallet = imp_sub.add_parser(
        "wallet", help="从链上地址导入加密资产 (轻钱包: tokenlist + balanceOf)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "支持链: eth, bsc, polygon, arbitrum, avalanche\n"
            "示例:\n"
            "  tracker import wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n"
            "  tracker import wallet bsc 0x... --base-currency USDT --dry-run\n"
            "  tracker import wallet polygon 0x... --tokenlist my_tokens.json\n"
        ),
    )
    p_i_wallet.add_argument("chain", help="链名称: eth / bsc / polygon / arbitrum / avalanche")
    p_i_wallet.add_argument("address", help="链上地址 (0x + 40 位十六进制)")
    p_i_wallet.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_i_wallet.add_argument("--tokenlist", default=None, help="外部 tokenlist JSON 路径 (覆盖内置列表)")
    p_i_wallet.add_argument("--base-currency", default="USD", help="计价货币 (默认 USD)")
    p_i_wallet.add_argument("--overwrite", action="store_true",
                            help="覆盖全部持仓 (缺省追加合并)")
    p_i_wallet.add_argument("--dry-run", action="store_true", help="仅查询预览, 不写入")
    p_i_wallet.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_wallet.set_defaults(func=cmd_import)

    p_i_file = imp_sub.add_parser(
        "file", help="从券商导出文件导入 (A股 CSV/Excel)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker import file 持仓.csv                  # 追加合并\n"
            "  tracker import file 持仓.xlsx --overwrite     # 覆盖全部持仓\n"
            "  tracker import file 持仓.csv --dry-run        # 仅解析预览\n"
        ),
    )
    p_i_file.add_argument("file", help="券商导出的持仓文件路径 (CSV/Excel)")
    p_i_file.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_i_file.add_argument("--overwrite", action="store_true",
                          help="覆盖全部持仓 (缺省追加合并)")
    p_i_file.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_i_file.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_file.set_defaults(func=cmd_import)

    # ---- sync (兼容入口, 已并入 import ibkr) ----
    p_sync = sub.add_parser(
        "sync", help="从 IB Gateway 账户同步持仓 (已并入 import ibkr)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker sync --dry-run                # 预览同步结果, 不写入\n"
            "  tracker sync --mode paper             # 模拟账户 (端口 4002)\n"
            "  tracker sync --mode live --json       # 实盘账户 + JSON 输出\n"
            "  tracker sync --append                 # 追加合并 (缺省覆盖全部持仓)\n"
        ),
    )
    p_sync.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_sync.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_sync.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置")
    p_sync.add_argument("--append", action="store_true",
                        help="追加合并 (按代码更新/新增); 缺省覆盖全部持仓")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # ---- import-wallet (兼容入口, 已并入 import wallet) ----
    p_wallet = sub.add_parser(
        "import-wallet", help="从链上地址导入加密资产 (已并入 import wallet)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "支持链: eth, bsc, polygon, arbitrum, avalanche\n"
            "示例:\n"
            "  tracker import-wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n"
            "  tracker import-wallet bsc 0x... --base-currency USDT\n"
            "  tracker import-wallet eth 0x... --add --json\n"
            "  tracker import-wallet polygon 0x... --tokenlist my_tokens.json --add\n"
        ),
    )
    p_wallet.add_argument("chain", help="链名称: eth / bsc / polygon / arbitrum / avalanche")
    p_wallet.add_argument("address", help="链上地址 (0x + 40 位十六进制)")
    p_wallet.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_wallet.add_argument("--tokenlist", default=None, help="外部 tokenlist JSON 路径 (覆盖内置列表)")
    p_wallet.add_argument("--base-currency", default="USD", help="计价货币 (默认 USD)")
    p_wallet.add_argument("--add", action="store_true", help="将余额追加写入 portfolio.json")
    p_wallet.add_argument("--overwrite", action="store_true",
                          help="覆盖全部持仓 (与 --add 互斥语义, 指定即写入)")
    p_wallet.add_argument("--json", action="store_true", help="输出 JSON")
    p_wallet.set_defaults(func=cmd_import_wallet)

    # ---- cache ----
    p_c = sub.add_parser(
        "cache", help="行情磁盘缓存管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker cache info          # 查看缓存统计\n"
            "  tracker cache clear         # 清空行情缓存\n"
            "  tracker cache info --json   # JSON 输出\n"
        ),
    )
    p_c.add_argument("action", nargs="?", choices=["info", "clear"], default="info")
    p_c.add_argument("--json", action="store_true")
    p_c.set_defaults(func=cmd_cache)

    return ap


def main(argv=None) -> None:
    """CLI 入口: 解析参数并分发到对应子命令."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
