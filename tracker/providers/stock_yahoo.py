"""Yahoo 股票行情与历史源 (global/cn/index 域共用).

行情走 Yahoo v7 ``finance/quote`` 批量接口 (100 个/请求, 共享 yfinance 的
YfData 会话/crumb); 历史K线仍走 OpenBB ``equity.price.historical``。

从 global_stocks 拆出: 这些函数不依赖 GlobalStocksProvider 的源链, 但被
global/cn/index/orchestration/mcap 多处复用。集中在此避免「CN 域从全球
provider 导入私有函数」的跨模块耦合。
"""

from __future__ import annotations

import math
import threading
import time

import pandas as pd

from ..deps import openbb as _obb
from ..symbols import Market, ParsedSymbol, is_currency_code, is_pence
from ..util import with_timeout
from .base import Quote, openbb_history_frame


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


# ---------- yfinance (OpenBB) 源 ----------


def _quote_from_dump(p: ParsedSymbol, d: dict) -> Quote | None:
    # yfinance 部分证券 (多为 ETF) 的 quoteSummary 缺 last/close, 只有 open
    # (盘前/盘后/无成交场景): 逐级回退, open 视为最新参考价。
    # 只接受「有限正数」: 停牌/坏响应里的 0、NaN、负价不能当成有效行情
    # (会产出市值 0 / 触发全部下限阈值); 该字段无效时继续尝试下一级,
    # 全部无效才返回 None 交给逐代码源链/负缓存。
    price = None
    for key in ("last_price", "price", "close", "open"):
        v = d.get(key)
        if v is None:
            continue
        try:
            candidate = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate) and candidate > 0:
            price = candidate
            break
    if price is None:
        return None
    prev = _pick(d, "prev_close", "previous_close")
    chg = _pick(d, "change_percent", "percent_change")

    # 与价格字段同一守卫: 停牌/坏响应里的 NaN 会 float() 成 nan 而非 None,
    # 直接漏进 Quote 变成 "+NaN%" 行情 (其余数据源均有 pd.notna/有限性过滤)。
    def _finite_or_none(v: object) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    prev = _finite_or_none(prev)
    chg = _finite_or_none(chg)
    # yfinance 对 LSE 返回的价为便士且 currency=GBp; 若字段缺失, GB 市场按便士处理
    # 避免回退到 p.currency(GBP) 导致价格放大 100 倍
    raw_ccy = _pick(d, "currency")
    # 空字符串与缺失同口径: 只判 None 会让 currency="" 绕过 .L 便士兜底
    # (VOD.L 报 109.5 便士, 不 ÷100 就成了 £109.5), 且空币种查不到汇率。
    # 数据源还可能给 "-"/"NONE" 等无币种占位符 (东财 A 股 f172 就是 "-"):
    # 放行会让汇率链查到伪货币对, 必须与缺失同口径回退。
    raw_ccy = "" if raw_ccy is None else str(raw_ccy).strip()
    if not is_currency_code(raw_ccy):
        # .IL (LSE 国际挂盘) 证券以 USD/EUR 计价 (KAP.IL=USD): 币种缺失时不能按
        # LSE 主板惯例兜底 GBp, 便士兜底会把价格再缩小 100 倍
        if p.yahoo.endswith(".IL"):
            raw_ccy = "USD"
        elif p.market is Market.GB:
            raw_ccy = "GBp"
        else:
            raw_ccy = p.currency
    # B 股计价币种以本地规范为准 (沪B=USD / 深B=HKD): yfinance 对沪深 B 股
    # 常误报 currency=CNY, 按其转换会把美元/港币市值按 CNY 记账 (汇率差 7 倍)。
    # p.currency 由 symbols.parse 按 B 股代码段权威推导, 优先于数据源自报。
    # (港股人民币柜台 8 开头 5 位同样在 parse 里推导成 CNY, 但 yfinance 对该号段
    # 报的就是 CNY, 无需覆盖; 美元柜台 09xxx/41xxx 与港币主板代码同形, parse 不猜。)
    if p.is_b_share and raw_ccy.upper() == "CNY":
        raw_ccy = p.currency
    # 便士符号大小写混用 (GBp/GBX/gbx/…), 必须先判定再大写, 否则小写 "gbx"
    # 会被当作普通货币而漏掉 ÷100, 英股价格放大 100 倍
    if is_pence(raw_ccy):
        currency = "GBP"
        price = float(price) / 100
        if prev is not None:
            prev = float(prev) / 100
    else:
        currency = raw_ccy.upper()
        price = float(price)
        if prev is not None:
            prev = float(prev)
    if chg is None and prev is not None and prev > 0:
        chg = (price / prev - 1) * 100
    return Quote(
        symbol=p.yahoo,
        name=_pick(d, "name"),
        price=price,
        prev_close=prev,
        change_pct=float(chg) if chg is not None else None,
        currency=currency,
        source="yfinance",
    )


_QUOTE_TIMEOUT = 10.0  # v7 单代码含首次 crumb 握手, 留足余量; 批量失败由熔断兜底
_BATCH_TIMEOUT = 30.0  # 批量一次 100 个代码 (单个 HTTP 请求), 挂住则整批放弃
_HISTORY_TIMEOUT = 60.0  # 历史K线单码请求 (OpenBB 内部可能重试/半开连接)
_YAHOO_CHUNK = 100  # 单次 v7 quote 的代码数: 实测 100 只 ~3.5s, 200 只会触发 30s 超时
_CHUNK_PAUSE = 1.0  # 批间基础暂停 (s); 出现限流迹象 (空批) 时翻倍
_EMPTY_STREAK_GIVEUP = 2  # 连续空批: 判定已被限流, 放弃剩余批次 (逐代码源链兜底)
_YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

# ---------- 进程级限流熔断 ----------
#
# Yahoo 限流是 IP 级的: 批量请求被 429/超时后, 若逐代码源链继续对 600+ 只
# 代码各发一次请求, 既拖住保温线程数分钟, 又会把限流窗口越撞越长。
# 批量失败/连续空批时打开熔断, TTL 内跳过 yfinance (健康记录也少几百条
# 误伤式失败), 冷却结束后下一轮保温自动恢复。
_THROTTLE_TTL = 180.0
_throttle_until = 0.0
_throttle_lock = threading.Lock()


def quote_throttled() -> bool:
    """yfinance 行情是否处于限流冷却期."""
    with _throttle_lock:
        return time.time() < _throttle_until


def quote_throttle_remaining() -> float:
    """距冷却结束还有多少秒 (未冷却返回 0)."""
    with _throttle_lock:
        return max(0.0, _throttle_until - time.time())


def _trip_throttle(seconds: float = _THROTTLE_TTL) -> None:
    global _throttle_until
    with _throttle_lock:
        _throttle_until = max(_throttle_until, time.time() + seconds)


def _clear_throttle() -> None:
    global _throttle_until
    with _throttle_lock:
        _throttle_until = 0.0


def reset_quote_throttle() -> None:
    """清空熔断状态 (设置页切换数据源 / 测试隔离用)."""
    _clear_throttle()


_yf_data_singleton = None
_yf_data_lock = threading.Lock()


def _yf_data_client():
    """yfinance 共享 YfData 单例 (cookie/crumb/session 复用)."""
    global _yf_data_singleton
    if _yf_data_singleton is not None:
        return _yf_data_singleton
    with _yf_data_lock:
        if _yf_data_singleton is None:
            from yfinance.data import YfData

            _yf_data_singleton = YfData()
    return _yf_data_singleton


def _is_rate_limited(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return name == "YFRateLimitError" or "Too Many Requests" in text or "429" in text


def _yahoo_quote_json(symbols: list[str], timeout: float = _BATCH_TIMEOUT) -> dict:
    """批量 Yahoo v7 quote (一次请求多个代码) 原始 JSON.

    不用 OpenBB 的 ``equity.price.quote``: 它的 yfinance provider 内部对每个
    代码各调一次 ``Ticker.get_info()`` (quoteSummary + v7 quote 两条请求),
    50 只 = 100 个请求, 既慢又极易触发 429。v7 批量 100 只只发 1 个请求,
    实测首次 ~4s (含 crumb 握手), 复用 YfData 会话后 <1s。
    """
    params = {"symbols": ",".join(symbols), "formatted": "false"}
    return with_timeout(
        _yf_data_client().get_raw_json,
        timeout,
        _YAHOO_QUOTE_URL,
        params=params,
    )


def _quote_from_v7(p: ParsedSymbol, row: dict) -> Quote | None:
    """v7 quote 行 → Quote (复用 _quote_from_dump 的价格/币种/便士守卫)."""
    return _quote_from_dump(
        p,
        {
            "symbol": p.yahoo,
            "name": _pick(row, "longName", "shortName", "displayName"),
            # 交给 _quote_from_dump 的 price 回退链逐级校验有限正数
            "last_price": row.get("regularMarketPrice"),
            "price": row.get("postMarketPrice"),
            "close": row.get("preMarketPrice"),
            "open": row.get("regularMarketOpen"),
            "prev_close": _pick(
                row, "regularMarketPreviousClose", "previousClose"
            ),
            "change_percent": _pick(
                row,
                "regularMarketChangePercent",
                "postMarketChangePercent",
                "preMarketChangePercent",
            ),
            "currency": row.get("currency"),
        },
    )


def _yahoo_quote_rows(
    parsed: list[ParsedSymbol], timeout: float = _BATCH_TIMEOUT
) -> dict[str, Quote]:
    """一批代码 → {yahoo: Quote}; 一次 v7 请求, 限流/超时原样抛出."""
    if not parsed:
        return {}
    by_symbol = {p.yahoo: p for p in parsed}
    by_upper = {p.yahoo.upper(): p for p in parsed}
    try:
        payload = _yahoo_quote_json([p.yahoo for p in parsed], timeout)
    except Exception as e:  # noqa: BLE001 - 限流/超时统一打开熔断
        if isinstance(e, TimeoutError) or _is_rate_limited(e):
            _trip_throttle()
        raise
    rows = (payload.get("quoteResponse") or {}).get("result") or []
    out: dict[str, Quote] = {}
    for row in rows:
        sym = str(row.get("symbol") or "")
        p = by_symbol.get(sym) or by_upper.get(sym.upper())
        if p is None:
            continue
        q = _quote_from_v7(p, row)
        if q is not None:
            out[p.yahoo] = q
    return out


def _yahoo_quote(p: ParsedSymbol) -> Quote:
    remaining = quote_throttle_remaining()
    if remaining > 0:
        # 熔断期不发网络: 避免批量已被 429 后, 600+ 只代码再各撞一次
        raise RuntimeError(
            f"{p.yahoo}: yfinance 限流冷却中 ({remaining:.0f}s), 跳过回源"
        )
    q = _yahoo_quote_rows([p], _QUOTE_TIMEOUT).get(p.yahoo)
    if q is None:
        raise RuntimeError(f"{p.yahoo}: yfinance 无行情结果")
    _clear_throttle()
    return q


def _yahoo_batch(parsed: list[ParsedSymbol]) -> dict[str, Quote]:
    """Yahoo v7 批量行情: 100 个/请求 + 批间暂停 + 限流早停/熔断.

    每个请求只取 100 个代码 (URL 长度可控, 单块失败不拖垮全部), 连续空批
    或超时视为 IP 已被限流, 放弃剩余块并打开进程级熔断 (冷却期跳过
    yfinance, 冷却结束后下一轮保温自动恢复)。
    """
    out: dict[str, Quote] = {}
    if not parsed:
        return out
    if quote_throttled():
        return out
    empty_streak = 0
    for start in range(0, len(parsed), _YAHOO_CHUNK):
        if start:
            time.sleep(_CHUNK_PAUSE * (2 if empty_streak else 1))
        chunk = parsed[start : start + _YAHOO_CHUNK]
        try:
            rows = _yahoo_quote_rows(chunk, _BATCH_TIMEOUT)
            if rows:
                out.update(rows)
                empty_streak = 0
                _clear_throttle()
            else:
                empty_streak += 1
        except Exception:  # noqa: BLE001 - 单块失败继续下一块, 连续失败才熔断
            empty_streak += 1
        if empty_streak >= _EMPTY_STREAK_GIVEUP:
            # 连续空批/批量超时: 判定 IP 已被限流, 本轮不再逐代码重试
            _trip_throttle()
            break
    return out


def _yahoo_quote_currency(yahoo: str) -> str:
    """LSE 等代码的报价币种 (yfinance fast_info); 失败/缺失返回 ""。

    仅用于历史K线的便士折算判定, 任何异常都安全返回空串 (调用方按便士兜底)。
    fast_info 内部走网络且 requests 无 timeout, TCP 半开时可以挂到分钟级,
    而它是在 _yahoo_history 已经取到数据之后才调 —— 故套 with_timeout 硬超时。
    """
    try:
        import yfinance as yf

        ccy = with_timeout(
            lambda: yf.Ticker(yahoo).fast_info.get("currency"), _QUOTE_TIMEOUT
        )
        return str(ccy or "").strip()
    except Exception:
        return ""


def openbb_history(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
    *,
    ensure_volume: bool = False,
) -> pd.DataFrame:
    """OpenBB equity.price.historical 的通用包装 (股票/指数/加密共用)."""
    kwargs = {"symbol": symbol, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    # _obb() 在计时区外求值 (冷启动 import 不计入预算); 网络调用硬超时,
    # 避免半开连接/OpenBB 内部重试把 CLI/页面主线程无限挂住。
    res = with_timeout(_obb().equity.price.historical, _HISTORY_TIMEOUT, **kwargs)
    return openbb_history_frame(res, ensure_volume=ensure_volume)


def _yahoo_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    df = openbb_history(p.yahoo, start_date, end_date)
    if p.market is Market.GB:
        # LSE 惯例: 便士报价 (GBp/GBX, is_pence 命中) 或币种未知时 ÷100 折成
        # 英镑; 真正的英镑报价 (Yahoo 报 "GBP", 如 VUSA.L/VWRL.L 的 ETF) 与
        # 美元/欧元计价线 (CSPX.L/VWRA.L, Yahoo 报 USD) 按面值。
        # 注意: 这里不能先 .upper() —— "GBp" 大写后会与英镑 "GBP" 混同,
        # 把英镑计价 ETF 的历史价错误缩小 100 倍 (与实时行情不一致)。
        ccy = _yahoo_quote_currency(p.yahoo).strip()
        if p.yahoo.endswith(".IL"):
            pence = is_pence(ccy)
        else:
            pence = is_pence(ccy) or not ccy
        if pence:
            for c in ("open", "high", "low", "close"):
                if c in df.columns:
                    df[c] = df[c].astype(float) / 100
    return df
