"""akshare / 东财股票行情与历史源 (cn/global/index 域共用).

从 global_stocks 拆出: 东财单代码行情、A股/港股日 K 与缓存/单飞逻辑被
CN、全球港股兜底、orchestration 多处复用。集中在此避免跨域私有导入。
"""

from __future__ import annotations

import math
import threading
import time

import pandas as pd

from ..deps import akshare as _ak
from ..deps import requests as _requests
from ..symbols import Market, ParsedSymbol, is_currency_code
from ..util import with_timeout
from .base import Quote

# akshare 单代码日K (stock_zh_a_hist / stock_hk_hist): 接口不传 timeout,
# 给硬超时防挂死 (单代码请求通常 1-3s, 30s 覆盖慢网/重试)
_AK_HIST_TIMEOUT = 30.0
# 单代码实时行情失败占位 TTL: 东财不可达/限流时不反复打 (与全市场 spot 的
# 失败占位同思路, 现在是 per-symbol 粒度)
_AK_FAIL_TTL = 15.0

# ---------- 实时行情源: 东财 push2 单代码 (akshare 同源, per-symbol) ----------
#
# 为什么不用 akshare 的全市场 spot (stock_zh_a_spot_em / stock_hk_spot_em):
#   那两个接口内部 fetch_paginated_data 分页拉全市场 (A股 ~5926 只 = 60 页,
#   每页之间 sleep 0.5-1.5s), 单次实测 42-102s —— 全市场 spot 任何秒级
#   超时都必然失败, 「A股默认走 akshare / 港股 akshare 兜底」实际从未生效。
#   单代码 stock/get 一个请求拿齐 最新价/昨收/涨跌幅/名称/总市值/市净率,
#   实测 0.4-0.8s, 且比全市场表**多**给港股的总市值/市净率 (全市场 spot 的
#   最终列把这两个丢了)。数据口径仍是东财实时 (akshare 只是它的封装), 故
#   source 标签沿用 "akshare"。

_AK_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 东财对无 UA 的请求直接 RST: akshare 的 stock_bid_ask_em 就是这样失败的
_AK_QUOTE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_AK_QUOTE_TIMEOUT = 8.0
# 东财字段码: f43=最新价 f57=代码 f58=名称 f60=昨收
#              f116=总市值 f162=市盈率(动) f167=市净率 f170=涨跌幅(%)
#              f172=报价币种
# f172 必须取: 港股有 USD/CNY 计价柜台 (如 09188/82318), 只按代码后缀
# 推断会把 USD 价格当 HKD 乘汇率, 市值差约 7.8 倍。
# f162 口径是「动态 PE」, 不是 TTM (实测 A 股可用, 部分港股返回 "-"):
# 单独写入 Quote.pe_dynamic, 不与估值页的 pe_ttm 混用。
_AK_QUOTE_FIELDS = "f43,f57,f58,f60,f116,f162,f167,f170,f172"

# 单代码行情进程内缓存 (短 TTL): 同一代码紧接着的重复请求 (UI 渲染 + 后台
# 刷新) 只发一次; per-key 单飞去重并发首拉; 失败写短 TTL 占位防反复打东财
_AK_QUOTE_TTL = 5.0
_ak_quote_cache: dict[str, tuple[float, dict | None]] = {}
_ak_quote_lock = threading.Lock()
_ak_quote_inflight: dict[str, threading.Lock] = {}


def _ak_secid(p: ParsedSymbol) -> str:
    """ParsedSymbol → 东财 secid (市场段.代码).

    段号按**交易所后缀**判 (不能按代码首位: 沪市 B 股 900xxx 与深市创业板
    300xxx 首位都非 6, 而东财把 900xxx 归在上海段 1):
      .SS/.SH → 1 (沪市: 60x/601/603/605/688 科创 + 900xxx B股)
      .SZ     → 0 (深市: 000/001/002/003/300/301 + 200xxx B股)
      .BJ     → 0 (北交所 43x/83x/87x/92x, 东财挂在 0 段)
      .HK     → 116 + 5 位代码 (00700/82318)
    """
    if p.market is Market.HK:
        return f"116.{p.ak_code}"
    code = p.ak_code or p.yahoo.split(".")[0]
    prefix = "1" if p.yahoo.rsplit(".", 1)[-1] in ("SS", "SH") else "0"
    return f"{prefix}.{code}"


def _ak_quote_fetch(secid: str) -> dict | None:
    """东财 push2 单代码行情 data 字典; 任何失败/无数据返回 None."""
    try:
        r = _requests().get(
            _AK_QUOTE_URL,
            params={
                "fltt": "2",
                "invt": "2",
                "secid": secid,
                "fields": _AK_QUOTE_FIELDS,
            },
            headers=_AK_QUOTE_HEADERS,
            timeout=_AK_QUOTE_TIMEOUT,
        )
        j = r.json()
    except Exception:
        return None
    data = j.get("data") if isinstance(j, dict) else None
    return data if isinstance(data, dict) else None


def _ak_quote_raw(p: ParsedSymbol) -> dict | None:
    """带缓存/单飞/失败占位的单代码行情; 失败 None (调用方抛错降级下一源)."""
    secid = _ak_secid(p)
    now = time.time()
    with _ak_quote_lock:
        hit = _ak_quote_cache.get(secid)
        if hit:
            ts, data = hit
            if now - ts < (_AK_QUOTE_TTL if data else _AK_FAIL_TTL):
                return data
        gate = _ak_quote_inflight.setdefault(secid, threading.Lock())
    acquired = gate.acquire(blocking=False)
    try:
        if acquired:  # 本线程负责拉取; 其它线程等拉取方写回缓存后读结果
            data = _ak_quote_fetch(secid)
            with _ak_quote_lock:
                _ak_quote_cache[secid] = (now, data)
            return data
        gate.acquire()
        gate.release()  # 拉取方已 pop 此 gate: 立即释放, 不留持有者
    finally:
        if acquired:
            with _ak_quote_lock:
                if _ak_quote_inflight.get(secid) is gate:
                    _ak_quote_inflight.pop(secid, None)
            gate.release()
    # 未获 gate: 拉取方已完成, 重读缓存; 仍未命中 (失败) 则返回 None
    with _ak_quote_lock:
        hit = _ak_quote_cache.get(secid)
    if hit:
        ts, data = hit
        if now - ts < (_AK_QUOTE_TTL if data else _AK_FAIL_TTL):
            return data
    return None


def _ak_num(v) -> float | None:
    """东财字段值 → float; 缺失值 "-"/None/非数字 → None."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s in ("", "-", "None", "nan"):
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _akshare_quote(p: ParsedSymbol) -> Quote:
    """A股/北交所/港股实时行情 (东财单代码): 价格 + 昨收 + 涨跌幅 + 名称.

    顺带反推每股净资产 (市净率) 与总市值 (东财实时口径, 比 yfinance 的滞后
    财报新), 供 PB/市值阈值提醒; 缺失即 None, 由 fundamentals 链路兜底。
    """
    d = _ak_quote_raw(p)
    if not d:
        raise RuntimeError(f"{p.yahoo}: akshare 行情不可用 ({p.market.value})")
    # 停牌/无成交时东财最新价就是 "-": 与其余行情源一致地守卫, 无效即抛错
    # 回退 yfinance, 不让 0/NaN 报价污染盈亏/PB/涨跌幅
    price = _ak_num(d.get("f43"))
    if price is None or price <= 0:
        raise RuntimeError(f"{p.yahoo}: akshare 无有效最新价 ({d.get('f43')!r})")
    pb = _ak_num(d.get("f167"))
    market_cap = _ak_num(d.get("f116"))
    pe_dynamic = _ak_num(d.get("f162"))
    # 东财自报币种 (f172): 港股 USD/CNY 柜台不能按后缀推断成 HKD。
    # 但 A股/B股/北交所的 f172 是 "-" (币种由代码推导, B 股还可能被泛化),
    # 必须用 parse 的权威币种; 不校验会把 "-" 写进行情缓存, 汇率链随后把
    # 它拼成 "-CNY=X" 查到 USD/CNY 汇率, A 股市值被放大数倍。
    reported_ccy = str(d.get("f172") or "").strip().upper()
    if p.market in (Market.CN, Market.BJ):
        currency = p.currency
    else:
        currency = reported_ccy if is_currency_code(reported_ccy) else p.currency
    return Quote(
        symbol=p.yahoo,
        name=str(d.get("f58") or "").strip() or None,
        price=price,
        prev_close=_ak_num(d.get("f60")),
        change_pct=_ak_num(d.get("f170")),
        currency=currency,
        book_value=price / pb if pb is not None and pb > 0 else None,
        market_cap=market_cap if market_cap is not None and market_cap > 0 else None,
        pe_dynamic=pe_dynamic if pe_dynamic is not None and pe_dynamic > 0 else None,
        source="akshare",
    )


def _akshare_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    ak = _ak()
    start = start_date.replace("-", "")
    end = end_date.replace("-", "") if end_date else None
    if p.market in (Market.CN, Market.BJ):
        kwargs = {
            "symbol": p.yahoo.split(".")[0],
            "period": "daily",
            "start_date": start,
            "adjust": "qfq",
        }
        if end:
            kwargs["end_date"] = end
        # 硬超时: akshare 的 requests.get 不传 timeout, TCP 半开时可挂到分钟级,
        # 而 get_history 跑在主线程 (CLI history/kline/compare) —— 整条命令卡死。
        df = with_timeout(ak.stock_zh_a_hist, _AK_HIST_TIMEOUT, **kwargs)
    else:
        ak_code = p.ak_code or ""
        if not ak_code or not ak_code.isdigit():
            raise ValueError(f"{p.yahoo}: 无效港股代码 (ak_code={ak_code!r})")
        kwargs = {
            "symbol": ak_code,
            "period": "daily",
            "start_date": start,
            "adjust": "qfq",
        }
        if end:
            kwargs["end_date"] = end
        df = with_timeout(ak.stock_hk_hist, _AK_HIST_TIMEOUT, **kwargs)
    # 两个分支统一守卫: akshare 无数据时返回完全无列的空表, 直接走到下面
    # rename/to_datetime 会抛裸 KeyError 'date' (原来只有 HK 分支有守卫,
    # A股分支的报错变成 "历史数据获取失败 ('date')", 看不出是「东财无该股
    # 历史」); 非空但缺日期/收盘列 (接口 schema 变更) 同样给可读错误。
    if df.empty:
        raise RuntimeError(f"{p.yahoo}: akshare 无历史数据")
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
    )
    if "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError(f"{p.yahoo}: akshare 历史数据缺少日期/收盘列")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    return df[keep]


