"""统一命令行入口: python -m tracker.cli <子命令> [选项]

子命令:
  snapshot  组合 + 自选快照 (持仓 / 自选提醒 / 阈值触发; --save 落历史净值)
  nav       历史净值 (读取 / 保存今日快照 / 按当前持仓回填)
  quote     查询单个或多个代码实时行情
  watchlist 自选股管理 (list / add / remove / triggered) 与阈值提醒
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

import importlib
import os
import sys

from ..storage import (  # noqa: F401 - 兼容再导出 (测试 monkeypatch 默认路径)
    PORTFOLIO_PATH as DEFAULT_PORTFOLIO,
    CorruptDataError,
    DataFileLocked,
)
from ..watchlist import DEFAULT_WATCHLIST  # noqa: F401 - 兼容再导出
from ._common import VERSION, _finish_with_error, months_arg  # noqa: F401 - 兼容再导出
from .parser import (  # noqa: F401 - build_parser / 校验器为既有公开入口
    CRYPTO_SOURCE_CHOICES,
    build_parser,
    positive_int,
)


def __getattr__(name: str):
    """兼容 `cli.prices` 等既有属性访问 (测试 monkeypatch / 旧调用方)."""
    if name == "prices":
        value = importlib.import_module(".prices", __name__.rsplit(".", 1)[0])
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
