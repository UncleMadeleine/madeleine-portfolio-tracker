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
  search    代码/名称搜索 → 规范代码 (结果可直接喂 kline/compare/quote)
  compare   多股走势/市值对比 (归一化折线 --metric mcap 可切市值; --json 出序列)
  import    统一持仓导入 (ibkr / longport / wallet / file, 追加合并或 --overwrite 覆盖)
  longport-login 长桥 OAuth 登录 (浏览器授权, token 自动缓存刷新)
  eodhd     EODHD BYOK 配置状态与连通性自检 (token 自备, 可选付费源)
  sync      从 IB Gateway 账户同步持仓 (已并入 import ibkr, 保留兼容)
  index-kline 宏观/风险指数K线 (IX.<KEY>, 独立于股票 kline)
  calendar  财报/重大事件日历 (组合+自选, 默认本月+下月)
  init      首次使用: 把包内配置模板复制到数据目录 (pip 安装后必走一步)

所有子命令均支持 --json 输出机器可读结果, 便于脚本与 AI 消费。
"""

from __future__ import annotations

import argparse
import os
import sys

# 暴露 prices 供测试 monkeypatch (cli.prices.get_quotes)
from .. import prices  # noqa: F401
from ..storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO, CorruptDataError, DataFileLocked
from ..watchlist import DEFAULT_WATCHLIST  # storage 门面 (watchlist.json 常量)
from ._common import VERSION, _finish_with_error
from .cache import cmd_cache
from .calendar import cmd_calendar, months_arg
from .eodhd import cmd_eodhd
from .export import cmd_export
from .compare import cmd_compare
from .init import cmd_init
from .search import cmd_search
from .fx import cmd_fx
from .history import cmd_history
from .kline import cmd_kline
from .index_kline import cmd_index_kline
from .portfolio import cmd_portfolio
from .quote import cmd_quote
from .report import cmd_report
from .snapshot import cmd_snapshot
from .sync import cmd_sync
from .importer import cmd_import
from .wallet import cmd_import_wallet
from .watchlist import cmd_watchlist
from .longport_login import cmd_longport_login

# 加密货币域数据源偏好 (与 tracker.storage.CRYPTO_SOURCES 同一组取值):
# auto = 默认链, 其余指定源前置, 链内其余源按默认相对顺序兜底
CRYPTO_SOURCE_CHOICES = ("auto", "binance", "gate", "hyperliquid", "yfinance")


def _add_crypto_source_arg(p: argparse.ArgumentParser) -> None:
    """给子命令注册 --crypto-source (加密货币域取数偏好; 只影响 crypto 代码)."""
    p.add_argument(
        "--crypto-source",
        dest="crypto_source",
        choices=CRYPTO_SOURCE_CHOICES,
        default="auto",
        help="加密货币数据源优先 (默认 auto: Binance→Gate.io→Hyperliquid→yfinance)",
    )


def positive_int(value: str) -> int:
    """正整数校验: --months/--rows/--limit 传 0/负值时给 argparse 类型错误.

    不校验的后果: --rows -1 会被 df.tail(-1) 解释成「除最后一行外全部」
    静默丢最新一根K线; --months 0/-5 会把数据源噪音 (Yahoo "start date
    cannot be after end date") 当错误抛给用户; --limit 0 把参数错误伪装成
    「无匹配结果」。
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"需为整数: {value!r}") from e
    if n < 1:
        raise argparse.ArgumentTypeError(f"需为正整数 (>=1), 得到: {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    """构建顶层 argparse 解析器, 注册全部子命令."""
    ap = argparse.ArgumentParser(
        prog="tracker",
        description="Madeleine Portfolio Tracker 命令行工具 (持仓 / 自选 / 行情 / 汇率 / 缓存)",
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
        "snapshot",
        help="组合 + 自选快照",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker snapshot                      # 查看全部持仓 + 自选\n"
            "  tracker snapshot --base USD           # 临时以 USD 为基础货币\n"
            "  tracker snapshot --watchlist 科技 --json  # 仅看「科技」列表并输出 JSON\n"
        ),
    )
    p_snap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_snap.add_argument(
        "--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径"
    )
    p_snap.add_argument(
        "--watchlist", default=None, help="只查看某个子自选列表 (默认全部)"
    )
    p_snap.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    p_snap.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    p_snap.add_argument(
        "--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)"
    )
    p_snap.add_argument(
        "--longport", action="store_true", help="优先使用长桥行情 (需 longport.json)"
    )
    _add_crypto_source_arg(p_snap)
    p_snap.add_argument("--json", action="store_true", help="输出 JSON")
    p_snap.set_defaults(func=cmd_snapshot)

    # ---- quote ----
    p_q = sub.add_parser(
        "quote",
        help="查询实时行情",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker quote AAPL 600519.SS          # 查询多个代码\n"
            "  tracker quote 0700.HK --akshare       # 港股优先 akshare\n"
            "  tracker quote AAPL --refresh          # 忽略 5min 磁盘缓存/负缓存强制刷新\n"
            "  tracker quote NVDA --json             # JSON 输出\n"
        ),
    )
    p_q.add_argument("symbols", nargs="+", help="Yahoo 代码, 如 AAPL 600519.SS BTC-USD")
    p_q.add_argument("--akshare", action="store_true")
    p_q.add_argument("--ibkr", action="store_true")
    p_q.add_argument("--longport", action="store_true")
    _add_crypto_source_arg(p_q)
    p_q.add_argument(
        "--refresh",
        action="store_true",
        help="忽略 5min 磁盘行情缓存与失败负缓存, 强制重新取数",
    )
    p_q.add_argument("--json", action="store_true")
    p_q.set_defaults(func=cmd_quote)

    # ---- watchlist ----
    p_w = sub.add_parser(
        "watchlist",
        help="自选股管理 (list/add/remove) 与阈值提醒",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker watchlist list                          # 查看全部自选 + 行情\n"
            "  tracker watchlist list --no-quotes             # 仅展示配置\n"
            "  tracker watchlist add AAPL --list 科技 --upper1 250 --note 苹果\n"
            "  tracker watchlist add 600519.SS --list 白酒 --lower1 1500\n"
            "  tracker watchlist add 600036.SS --metric pb --upper1 1.2 --lower1 0.8\n"
            "  tracker watchlist add AAPL --metric mcap --upper1 4000000000000\n"
            "  tracker watchlist remove AAPL --list 科技       # 仅从「科技」列表移除\n"
            "  tracker watchlist import-longport --dry-run     # 预览长桥自选导入\n"
            "  tracker watchlist import-longport --overwrite   # 覆盖现有自选\n"
        ),
    )
    p_w.add_argument(
        "action",
        nargs="?",
        choices=["list", "add", "remove", "import-longport"],
        default="list",
    )
    p_w.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_w.add_argument(
        "--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径"
    )
    p_w.add_argument(
        "--list",
        dest="wl_list",
        default=None,
        help="所属列表名 (add 时指定 / list 时过滤)",
    )
    p_w.add_argument("--upper1", type=float, help="上限 I")
    p_w.add_argument("--upper2", type=float, help="上限 II")
    p_w.add_argument("--lower1", type=float, help="下限 I")
    p_w.add_argument("--lower2", type=float, help="下限 II")
    p_w.add_argument(
        "--metric",
        choices=["price", "pb", "mcap"],
        default=None,
        help="阈值基准指标: price=现价 (默认) / pb=市净率 / mcap=总市值",
    )
    p_w.add_argument("--note", help="备注")
    p_w.add_argument("--akshare", action="store_true")
    p_w.add_argument("--ibkr", action="store_true")
    p_w.add_argument("--longport", action="store_true")
    _add_crypto_source_arg(p_w)
    p_w.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="import-longport: 仅预览, 不写入",
    )
    p_w.add_argument(
        "--overwrite",
        action="store_true",
        help="import-longport: 清空现有自选后写入 (写前备份 .bak)",
    )
    p_w.add_argument("--json", action="store_true", help="输出 JSON")
    p_w.add_argument(
        "--no-quotes", action="store_true", help="list 时不拉行情, 仅展示配置"
    )
    p_w.set_defaults(func=cmd_watchlist)

    # ---- portfolio ----
    p_p = sub.add_parser(
        "portfolio",
        help="持仓管理 (list/add/remove/set-base)",
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
    p_p.add_argument(
        "action",
        nargs="?",
        choices=["list", "add", "remove", "set-base"],
        default="list",
    )
    p_p.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_p.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_p.add_argument("--quantity", type=float, help="持仓数量 (add 时必填)")
    p_p.add_argument("--avg-cost", type=float, help="成本价 (当地货币, add 时可选)")
    p_p.add_argument(
        "--currency",
        help="基础货币 (set-base 时指定, 如 CNY/USD); 也可作为位置参数: set-base USD",
    )
    p_p.add_argument("--json", action="store_true", help="输出 JSON")
    p_p.set_defaults(func=cmd_portfolio)

    # ---- report ----
    p_r = sub.add_parser(
        "report",
        help="导出自选监控阈值报告 (md/csv/json)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker report -f md                    # 终端打印 Markdown 报告\n"
            "  tracker report -f csv -o report.csv     # 导出 CSV 文件\n"
            "  tracker report -f json -w 科技           # 仅「科技」列表, JSON 输出\n"
            "  tracker report -f md --sort severity     # 按严重度排序\n"
        ),
    )
    p_r.add_argument(
        "--format",
        "-f",
        choices=["md", "csv", "json"],
        default="md",
        help="输出格式 (默认 md)",
    )
    p_r.add_argument(
        "--watchlist", "-w", default=None, help="只导出某个子自选列表 (默认全部)"
    )
    p_r.add_argument(
        "--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径"
    )
    p_r.add_argument(
        "--sort",
        choices=["default", "severity", "change_desc", "change_asc"],
        default="default",
        help="列表内排序方式",
    )
    p_r.add_argument("--akshare", action="store_true")
    p_r.add_argument("--ibkr", action="store_true")
    p_r.add_argument("--longport", action="store_true")
    _add_crypto_source_arg(p_r)
    p_r.add_argument(
        "--output", "-o", default=None, help="输出文件路径 (缺省打印到终端)"
    )
    p_r.set_defaults(func=cmd_report)

    # ---- export ----
    p_e = sub.add_parser(
        "export",
        help="导出投资组合快照为 CSV/JSON/Markdown 报表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker export -f json -o snapshot.json   # 导出完整快照 JSON\n"
            "  tracker export -f csv -o holdings.csv      # 导出持仓 CSV\n"
            "  tracker export -f md -o snapshot.md         # 导出 Markdown 报表\n"
            "  tracker export -f md --base USD            # 临时以 USD 为基础货币\n"
        ),
    )
    p_e.add_argument(
        "--format",
        "-f",
        choices=["csv", "json", "md"],
        default="json",
        help="输出格式 (默认 json)",
    )
    p_e.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_e.add_argument(
        "--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径"
    )
    p_e.add_argument(
        "--watchlist", "-w", default=None, help="只导出某个子自选列表 (默认全部)"
    )
    p_e.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    p_e.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    p_e.add_argument(
        "--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)"
    )
    p_e.add_argument(
        "--longport", action="store_true", help="优先使用长桥行情 (需 longport.json)"
    )
    _add_crypto_source_arg(p_e)
    p_e.add_argument(
        "--output", "-o", default=None, help="输出文件路径 (缺省打印到终端)"
    )
    p_e.set_defaults(func=cmd_export)

    # ---- fx ----
    p_fx = sub.add_parser(
        "fx",
        help="汇率查询",
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
    p_fx.add_argument("--longport", action="store_true", help="预留: 长桥暂不支持汇率")
    p_fx.add_argument("--json", action="store_true")
    p_fx.set_defaults(func=cmd_fx)

    # ---- history ----
    p_h = sub.add_parser(
        "history",
        help="历史价格 (近 N 个月)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker history AAPL                  # 近 12 个月日线\n"
            "  tracker history 600519.SS --months 24  # 近 24 个月\n"
            "  tracker history AAPL --json            # JSON 输出\n"
        ),
    )
    p_h.add_argument("symbol")
    p_h.add_argument("--months", type=positive_int, default=12)
    p_h.add_argument(
        "--rows", type=positive_int, default=10, help="表格模式打印最近 N 行"
    )
    p_h.add_argument("--akshare", action="store_true")
    p_h.add_argument(
        "--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)"
    )
    p_h.add_argument(
        "--longport", action="store_true", help="优先使用长桥行情 (需 longport.json)"
    )
    _add_crypto_source_arg(p_h)
    p_h.add_argument("--json", action="store_true")
    p_h.set_defaults(func=cmd_history)

    # ---- kline ----
    p_k = sub.add_parser(
        "kline",
        help="K线蜡烛图 (生成交互式 HTML, 含成交量/均线)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker kline AAPL                              # 日K + 成交量 + MA5/20/60\n"
            "  tracker kline 600519.SS --period weekly         # 周K\n"
            "  tracker kline AAPL --ma 10,30 --refresh          # 自定义均线 + 强制刷新\n"
            "  tracker kline AAPL --no-volume --open            # 无成交量 + 浏览器打开\n"
        ),
    )
    p_k.add_argument(
        "symbol",
        help="Yahoo 代码或名称片段 (非法代码自动搜索回退), 如 AAPL 0700.HK 腾讯",
    )
    p_k.add_argument(
        "--months", type=positive_int, default=12, help="拉取近 N 个月日线"
    )
    p_k.add_argument(
        "--period",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="K线周期 (默认日K)",
    )
    p_k.add_argument(
        "--ma", default="5,20,60", help="均线周期, 逗号分隔 (如 5,10,20,60)"
    )
    p_k.add_argument("--no-volume", action="store_true", help="隐藏成交量副图")
    p_k.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    p_k.add_argument("--akshare", action="store_true")
    p_k.add_argument(
        "--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)"
    )
    p_k.add_argument(
        "--longport", action="store_true", help="优先使用长桥行情 (需 longport.json)"
    )
    _add_crypto_source_arg(p_k)
    p_k.add_argument(
        "--output",
        "-o",
        default=None,
        help="HTML 输出路径 (默认 var/kline_<代码>.html)",
    )
    p_k.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="生成后自动在浏览器打开",
    )
    p_k.add_argument("--json", action="store_true", help="输出 JSON 数据 (不生成图表)")
    p_k.set_defaults(func=cmd_kline)

    # ---- calendar ----
    p_cal = sub.add_parser(
        "calendar",
        help="组合/自选的财报与重大事件日历 (默认本月+下月)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker calendar                       # 持仓+自选, 本月+下月\n"
            "  tracker calendar --months 3 --json     # 未来三个月, JSON 输出\n"
            "  tracker calendar --watchlist 科技       # 组合持仓 + 「科技」列表\n"
            "  tracker calendar AAPL 0700.HK 600519.SS # 指定代码\n"
            "  tracker calendar --refresh             # 忽略 6h/24h 缓存强制刷新\n"
            "  tracker calendar --longport            # 开长桥兜底 (需已 longport-login)\n"
        ),
    )
    p_cal.add_argument(
        "symbols", nargs="*", help="代码列表 (缺省 = 持仓+自选全部), 如 AAPL 600519.SS"
    )
    p_cal.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_cal.add_argument(
        "--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径"
    )
    p_cal.add_argument(
        "--watchlist",
        default=None,
        help="自选部分只看某个列表 (组合持仓始终包含; 缺省: 全部自选)",
    )
    p_cal.add_argument(
        "--months",
        type=months_arg,
        default=2,
        help="从本月起的自然月数 1..12 (默认 2 = 本月+下月)",
    )
    p_cal.add_argument(
        "--refresh", action="store_true", help="忽略缓存强制刷新"
    )
    p_cal.add_argument(
        "--longport",
        action="store_true",
        help="开启长桥日历兜底 (yfinance/akshare 拿不到时补; 需已 longport-login)"
    )
    p_cal.add_argument("--json", action="store_true", help="输出 JSON")
    p_cal.set_defaults(func=cmd_calendar)

    # ---- search ----
    p_s = sub.add_parser(
        "search",
        help="代码/名称搜索 → 规范代码 (可直接喂 kline/compare/quote)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker search 700                    # 纯数字: 补零命中港股 0700.HK\n"
            "  tracker search 茅台 --json            # 中文名, JSON 输出\n"
            "  tracker search AAPL --exact           # 宽松输入→唯一最佳匹配\n"
            "  tracker search 腾讯 | jq -r '.results[0].code'  # 结果直接喂 kline\n"
        ),
    )
    p_s.add_argument("query", help="代码片段 / 中文名 / 英文名, 如 700 腾讯 maotai")
    p_s.add_argument(
        "--limit", type=positive_int, default=10, help="最多返回条数 (默认 10)"
    )
    p_s.add_argument(
        "--exact",
        action="store_true",
        help="宽松解析: 合法代码直接归一, 其余取最佳匹配 (≤1 条/次)",
    )
    p_s.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON (results[].code 可直接喂取数命令)",
    )
    p_s.set_defaults(func=cmd_search)

    # ---- compare ----
    p_cmp = sub.add_parser(
        "compare",
        help="多股走势/市值对比 (--metric mcap 切市值; --json 出序列, 默认出交互式 HTML)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker compare AAPL 0700.HK 600519.SS          # 归一化对比 (起点=100)\n"
            "  tracker compare AAPL NVDA --months 6 --period weekly\n"
            "  tracker compare AAPL 0700.HK --raw               # 不归一化 (各代码原币种)\n"
            "  tracker compare AAPL NVDA --json                 # JSON: 每代码归一化序列+涨跌\n"
            "  tracker compare 601155.SS 1030.HK --ratio 601155.SS 1030.HK\n"
            "                                                 # 额外画比例线 (默认原币比值; --ratio-fx 汇率调整)\n"
            "  tracker compare 0700.HK 600519.SS --metric mcap  # 市值对比 (统一折算基础货币)\n"
            "  tracker compare AAPL NVDA --metric mcap --currency USD  # 市值对比指定折算币种\n"
        ),
    )
    p_cmp.add_argument(
        "symbols", nargs="+", help="2+ 个代码/名称片段 (宽松输入, 自动搜索回退)"
    )
    p_cmp.add_argument(
        "--months", type=positive_int, default=12, help="对比近 N 个月 (默认 12)"
    )
    p_cmp.add_argument(
        "--period",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="周期 (默认日K)",
    )
    p_cmp.add_argument(
        "--metric",
        choices=["price", "mcap"],
        default="price",
        help="对比指标: price=收盘价 (默认); mcap=历史市值 (A股用东财历史总市值, "
        "其余市场用历史股本×收盘价, 缺失时按当前市值近似; crypto/指数无市值语义)",
    )
    p_cmp.add_argument(
        "--raw", action="store_true", help="不归一化, 直接画各代码原币种收盘价"
    )
    p_cmp.add_argument(
        "--ratio",
        nargs=2,
        metavar=("分子", "分母"),
        default=None,
        help="额外画比例线: 两代码收盘价相除 (需在对比代码中), 如 --ratio 601155.SS 1030.HK",
    )
    p_cmp.add_argument(
        "--ratio-fx",
        action="store_true",
        help="比例线按历史汇率调整 (跨币种时把分母换算成分子币种; 默认关闭; 市值模式已统一币种, 无需此开关)",
    )
    p_cmp.add_argument(
        "--currency",
        metavar="币种",
        default=None,
        help="市值对比 (--metric mcap) 统一折算币种 (默认组合基础货币, 如 CNY/USD)",
    )
    p_cmp.add_argument("--akshare", action="store_true")
    p_cmp.add_argument(
        "--refresh",
        action="store_true",
        help="忽略 K线/市值/基本面/汇率缓存与负缓存, 强制重新取数",
    )
    p_cmp.add_argument(
        "--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)"
    )
    p_cmp.add_argument(
        "--longport", action="store_true", help="优先使用长桥行情 (需 longport.json)"
    )
    _add_crypto_source_arg(p_cmp)
    p_cmp.add_argument(
        "--output",
        "-o",
        default=None,
        help="HTML 输出路径 (默认 var/compare_<代码>.html)",
    )
    p_cmp.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="生成后自动在浏览器打开",
    )
    p_cmp.add_argument(
        "--json", action="store_true", help="输出 JSON (每代码归一化序列 + 区间涨跌)"
    )
    p_cmp.set_defaults(func=cmd_compare)

    # ---- index-kline ----
    p_ik = sub.add_parser(
        "index-kline",
        help="宏观/风险指数K线 (IX.<KEY>, 独立于股票 kline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker index-kline IX.DXY --months 12\n"
            "  tracker index-kline IX.VIX --period weekly --json\n"
            "  tracker index-kline IX.URANIUM --months 60\n"
            "  tracker index-kline IX.CSI300 --list\n"
        ),
    )
    p_ik.add_argument(
        "symbol",
        nargs="?",
        default=None,
        help="指数代码 IX.<KEY>, 如 IX.DXY / IX.VIX / IX.URANIUM / IX.CSI300",
    )
    p_ik.add_argument("--list", action="store_true", help="列出已收录指数目录")
    p_ik.add_argument(
        "--months", type=positive_int, default=12, help="拉取近 N 个月日线"
    )
    p_ik.add_argument(
        "--period",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="K线周期 (默认日K)",
    )
    p_ik.add_argument(
        "--ma", default="5,20,60", help="均线周期, 逗号分隔 (如 5,10,20,60)"
    )
    p_ik.add_argument(
        "--volume", action="store_true", help="显示成交量副图 (指数一般无意义)"
    )
    p_ik.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    p_ik.add_argument(
        "--output",
        "-o",
        default=None,
        help="HTML 输出路径 (默认 var/index_kline_<代码>.html)",
    )
    p_ik.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="生成后自动在浏览器打开",
    )
    p_ik.add_argument("--json", action="store_true", help="输出 JSON 数据 (不生成图表)")
    p_ik.set_defaults(func=cmd_index_kline)

    # ---- import ----
    p_imp = sub.add_parser(
        "import",
        help="统一持仓导入 (ibkr / wallet / file)",
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
            "  tracker import ibkr --dry-run                     # 预览 IBKR 账户持仓\n"
            "  tracker import ibkr --mode live --overwrite       # 实盘账户, 覆盖写入\n"
            "  tracker import longport --dry-run                 # 预览长桥账户持仓\n"
        ),
    )
    imp_sub = p_imp.add_subparsers(dest="source", metavar="<来源>", required=True)

    p_i_ibkr = imp_sub.add_parser(
        "ibkr",
        help="从 IB Gateway 账户导入持仓",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker import ibkr --dry-run          # 仅预览\n"
            "  tracker import ibkr --mode paper       # 模拟账户 (端口 4002)\n"
            "  tracker import ibkr --overwrite        # 覆盖全部持仓\n"
        ),
    )
    p_i_ibkr.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_i_ibkr.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置",
    )
    p_i_ibkr.add_argument(
        "--overwrite", action="store_true", help="覆盖全部持仓 (缺省追加合并)"
    )
    p_i_ibkr.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_i_ibkr.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_ibkr.set_defaults(func=cmd_import)

    p_i_lp = imp_sub.add_parser(
        "longport",
        help="从长桥账户导入持仓 (OAuth 登录或 API Key)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker import longport --dry-run       # 仅预览\n"
            "  tracker import longport --overwrite     # 覆盖全部持仓\n"
            "登录方式: tracker longport-login 发起 OAuth 浏览器授权 (token 自动缓存刷新);\n"
            "或 longport.json 配置 auth=apikey 与 app_key/app_secret/access_token。"
        ),
    )
    p_i_lp.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_i_lp.add_argument(
        "--overwrite", action="store_true", help="覆盖全部持仓 (缺省追加合并)"
    )
    p_i_lp.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_i_lp.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_lp.set_defaults(func=cmd_import)

    p_i_wallet = imp_sub.add_parser(
        "wallet",
        help="从链上地址导入加密资产 (轻钱包: tokenlist + balanceOf)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "支持链: eth, bsc, polygon, arbitrum, avalanche, tron, solana\n"
            "示例:\n"
            "  tracker import wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n"
            "  tracker import wallet bsc 0x... --base-currency USDT --dry-run\n"
            "  tracker import wallet tron TQxxxxxxxx... --dry-run\n"
            "  tracker import wallet solana Hxxxxxxxx... --dry-run\n"
        ),
    )
    p_i_wallet.add_argument(
        "chain", help="链名称: eth / bsc / polygon / arbitrum / avalanche / tron / solana"
    )
    p_i_wallet.add_argument(
        "address", help="链上地址 (EVM: 0x+40位hex; TRON: 'T'开头; Solana: base58 公钥)"
    )
    p_i_wallet.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_i_wallet.add_argument(
        "--tokenlist", default=None, help="外部 tokenlist JSON 路径 (覆盖内置列表)"
    )
    p_i_wallet.add_argument(
        "--base-currency", default="USD", help="计价货币 (默认 USD)"
    )
    p_i_wallet.add_argument(
        "--overwrite", action="store_true", help="覆盖全部持仓 (缺省追加合并)"
    )
    p_i_wallet.add_argument("--dry-run", action="store_true", help="仅查询预览, 不写入")
    p_i_wallet.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_wallet.set_defaults(func=cmd_import)

    p_i_file = imp_sub.add_parser(
        "file",
        help="从券商导出文件导入 (A股 CSV/Excel)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker import file 持仓.csv                  # 追加合并\n"
            "  tracker import file 持仓.xlsx --overwrite     # 覆盖全部持仓\n"
            "  tracker import file 持仓.csv --dry-run        # 仅解析预览\n"
        ),
    )
    p_i_file.add_argument("file", help="券商导出的持仓文件路径 (CSV/Excel)")
    p_i_file.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_i_file.add_argument(
        "--overwrite", action="store_true", help="覆盖全部持仓 (缺省追加合并)"
    )
    p_i_file.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_i_file.add_argument("--json", action="store_true", help="输出 JSON")
    p_i_file.set_defaults(func=cmd_import)

    # ---- sync (兼容入口, 已并入 import ibkr) ----
    p_sync = sub.add_parser(
        "sync",
        help="从 IB Gateway 账户同步持仓 (已并入 import ibkr)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker sync --dry-run                # 预览同步结果, 不写入\n"
            "  tracker sync --mode paper             # 模拟账户 (端口 4002)\n"
            "  tracker sync --mode live --json       # 实盘账户 + JSON 输出\n"
            "  tracker sync --overwrite              # 覆盖全部持仓 (缺省追加合并)\n"
        ),
    )
    p_sync.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_sync.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_sync.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置",
    )
    p_sync.add_argument(
        "--append",
        action="store_true",
        help="追加合并 (按代码更新/新增) — 缺省行为, 保留兼容旧调用",
    )
    p_sync.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖全部持仓 (清空后重写, 写前自动备份 portfolio.json.bak)",
    )
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # ---- longport-login (长桥 OAuth 登录引导) ----
    p_lp = sub.add_parser(
        "longport-login",
        help="发起长桥 OAuth 登录 (浏览器授权, token 自动缓存与刷新)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "前置: longport.json 已配置有效 client_id (OAuth 客户端注册见 longport.example.json)。\n"
            "授权成功后 token 缓存于 ~/.longport/openapi/tokens/<client_id>, 自动刷新,\n"
            "之后 import longport / --longport 行情即可用, 无需重复登录。"
        ),
    )
    p_lp.add_argument(
        "--json",
        action="store_true",
        help="JSON 模式: 不自动开浏览器, 只输出授权 URL (status.ok)",
    )
    p_lp.add_argument(
        "--output",
        "-o",
        default=None,
        help="--json 时把授权 URL 写入指定文件 (供脚本消费)",
    )
    p_lp.set_defaults(func=cmd_longport_login)

    # ---- eodhd (BYOK 付费源: 配置状态 + 连通性自检) ----
    p_eod = sub.add_parser(
        "eodhd",
        help="EODHD BYOK 配置状态与连通性自检 (token 自备, 可选付费源)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "配置: 数据目录 eodhd.json (或 env EODHD_CONFIG 指定路径),\n"
            "token 也可用 env TRACKER_EODHD_TOKEN 注入 (优先级更高)。\n"
            "示例:\n"
            "  tracker eodhd status                 # 查看是否可用 / token 掩码\n"
            "  tracker eodhd status --json          # 机器可读\n"
            "  tracker eodhd test AAPL 0700.HK      # 实际打一次行情 + 最近 7 天历史 + 基本面\n"
            "  tracker eodhd test AAPL --json\n"
        ),
    )
    p_eod.add_argument(
        "action", nargs="?", choices=["status", "test"], default="status"
    )
    p_eod.add_argument("symbols", nargs="*", help="test 的目标代码 (可多个)")
    p_eod.add_argument(
        "--config", default=None, help="eodhd.json 路径 (默认数据目录/eodhd.json)"
    )
    p_eod.add_argument("--json", action="store_true", help="输出 JSON")
    p_eod.set_defaults(func=cmd_eodhd)

    # ---- import-wallet (兼容入口, 已并入 import wallet) ----
    p_wallet = sub.add_parser(
        "import-wallet",
        help="从链上地址导入加密资产 (已并入 import wallet)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "支持链: eth, bsc, polygon, arbitrum, avalanche, tron, solana\n"
            "示例:\n"
            "  tracker import-wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n"
            "  tracker import-wallet bsc 0x... --base-currency USDT\n"
            "  tracker import-wallet tron T... --add --json\n"
            "  tracker import-wallet solana H... --json               # 仅查询不写入 (缺省)\n"
            "  tracker import-wallet polygon 0x... --tokenlist my_tokens.json --add\n"
        ),
    )
    p_wallet.add_argument(
        "chain", help="链名称: eth / bsc / polygon / arbitrum / avalanche / tron / solana"
    )
    p_wallet.add_argument(
        "address", help="链上地址 (EVM: 0x+40位hex; TRON: 'T'开头; Solana: base58 公钥)"
    )
    p_wallet.add_argument(
        "--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径"
    )
    p_wallet.add_argument(
        "--tokenlist", default=None, help="外部 tokenlist JSON 路径 (覆盖内置列表)"
    )
    p_wallet.add_argument("--base-currency", default="USD", help="计价货币 (默认 USD)")
    # --add 与 --overwrite 必须互斥: 不同现时 wallet.py 里 --overwrite 静默
    # 胜出, 用户意图「追加」实际执行「覆盖全部持仓」, 且摘要无任何迹象
    p_wallet_mode = p_wallet.add_mutually_exclusive_group()
    p_wallet_mode.add_argument(
        "--add", action="store_true", help="将余额追加写入 portfolio.json"
    )
    p_wallet_mode.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖全部持仓 (与 --add 互斥, 指定即写入)",
    )
    p_wallet.add_argument("--json", action="store_true", help="输出 JSON")
    p_wallet.set_defaults(func=cmd_import_wallet)

    # ---- cache ----
    p_c = sub.add_parser(
        "cache",
        help="行情磁盘缓存管理",
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

    # ---- init (首次使用: 模板落地到数据目录) ----
    p_init = sub.add_parser(
        "init",
        help="把包内配置模板复制到数据目录 (首次使用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tracker init            # 生成缺失的 portfolio/watchlist/settings/ibkr/longport/eodhd.json\n"
            "  tracker init --force    # 用模板覆盖已存在的配置\n"
            "数据目录: $TRACKER_HOME > 源码树仓库根 > 平台用户目录 (~/.tracker)\n"
        ),
    )
    p_init.add_argument(
        "--force", action="store_true", help="用模板覆盖已存在的配置文件"
    )
    p_init.add_argument("--json", action="store_true", help="输出 JSON")
    p_init.set_defaults(func=cmd_init)

    return ap


def main(argv=None) -> None:
    """CLI 入口: 解析参数并分发到对应子命令."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "list", False) and args.command == "index-kline":
            from .index_kline import print_index_catalog

            print_index_catalog(as_json=args.json)
            return
        args.func(args)
    except CorruptDataError as e:
        _finish_with_error(str(e))
    except DataFileLocked as e:
        # 数据文件被其它进程持有 (读-改-写锁等待超时): 本次保存未执行,
        # 给可读错误而不是裸 traceback
        _finish_with_error(str(e))
    except BrokenPipeError:
        # 管道消费方提前关闭 (`tracker search 腾讯 | jq ...` 是官方推荐用法):
        # Python 默认在退出时 flush stdout 再抛一次 BrokenPipeError 并打
        # "Exception ignored" 噪音 + 退出码 120。把 stdout 指向 devnull 后
        # 按 SIGPIPE 惯例 (128+13) 退出。
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        raise SystemExit(141)
