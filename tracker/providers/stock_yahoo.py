"""Yahoo / OpenBB 股票行情与历史源 (global/cn/index 域共用).

从 global_stocks 拆出: 这些函数不依赖 GlobalStocksProvider 的源链, 但被
global/cn/index/orchestration/mcap 多处复用。集中在此避免「CN 域从全球
provider 导入私有函数」的跨模块耦合。
"""

from __future__ import annotations

import math
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


_QUOTE_TIMEOUT = 5.0  # yfinance/OpenBB 偶发内部重试会把单码失败拖到 8s+; 硬超时快速降级
_BATCH_TIMEOUT = 30.0  # 批量一次上百代码, 预算放大; 仍挂住则整批放弃走逐代码源链
_HISTORY_TIMEOUT = 60.0  # 历史K线单码请求 (OpenBB 内部可能重试/半开连接)
_YAHOO_CHUNK = 50  # 单批代码数: 一次上千代码的 quote 请求会被 Yahoo 429/401 拒掉
_CHUNK_PAUSE = 1.0  # 批间基础暂停 (s); 出现限流迹象 (空批) 时翻倍
_EMPTY_STREAK_GIVEUP = 2  # 连续空批: 判定已被限流, 放弃剩余批次 (逐代码源链兜底)


def _yahoo_quote(p: ParsedSymbol) -> Quote:
    # _obb() 必须先在计时区外求值: 冷启动 (首次 import openbb + 触达 provider
    # 注册表) 实测 8-16s, 整段塞进 with_timeout 会吃掉 5s 预算, 首次必然超时。
    # 取到引用后 5s 预算只覆盖真正的网络请求。
    res = with_timeout(
        _obb().equity.price.quote,
        _QUOTE_TIMEOUT,
        symbol=p.yahoo,
        provider="yfinance",
    )
    if not res.results:
        raise RuntimeError(f"{p.yahoo}: yfinance 无行情结果")
    d = res.results[0].model_dump()
    q = _quote_from_dump(p, d)
    if q is None:
        raise RuntimeError(f"{p.yahoo}: yfinance 无有效价格")
    return q


def _yahoo_batch(parsed: list[ParsedSymbol]) -> dict[str, Quote]:
    """yfinance 批量行情: 分块拉取 + 批间暂停 + 限流早停.

    一次性把全部代码 (自选 600+ 时) 塞进单个 quote 请求, Yahoo 会以
    429 (crumb rate-limited) / 401 (Invalid Crumb) 拒绝; 且限流是 IP 级 —
    随后单代码行情/K线历史请求全部遭殃。按块拆分 + 批间暂停; 连续空批视为
    已被限流, 放弃剩余块 (未取到的代码由逐代码源链兜底, 失败进负缓存,
    TTL 内不重试), 不再持续撞击数据源。
    """
    out: dict[str, Quote] = {}
    if not parsed:
        return out
    by_yahoo = {p.yahoo: p for p in parsed}
    empty_streak = 0
    # 与 _yahoo_quote 同理: _obb() 冷启动 (首次 import openbb + 触达 provider
    # 注册表) 必须在计时区外完成, 否则第一块请求还没发出预算已耗尽。取一次
    # 引用, 所有块共用 (openbb 是进程内单例)。
    try:
        quote_fn = _obb().equity.price.quote
    except Exception:
        # openbb 未装 / 版本不兼容 / 在非主线程初始化失败 (signal 限制):
        # 不能向上抛 —— orchestration.get_quotes 的域间循环没有保护, 一抛会
        # 让后面的 cn/crypto 域一起不执行 (违反「任一域失败不影响其它域」),
        # 且 CLI 批量查询/组合页直接崩。返回空表, 由逐代码源链兜底
        # (_fetch_domain 内每个源都单独 try, 失败进负缓存)。
        return out
    for start in range(0, len(parsed), _YAHOO_CHUNK):
        if start:
            time.sleep(_CHUNK_PAUSE * (2 if empty_streak else 1))
        chunk = parsed[start : start + _YAHOO_CHUNK]
        try:
            # 批量同样要硬超时: OpenBB/yfinance 偶发内部重试会把批量调用挂住,
            # 而它跑在 get_quotes 主线程里 (域间串行), 一挂全批卡死。超时按
            # 空结果处理, 后续逐代码源链还能兜底 (单代码 5s 硬超时)
            res = with_timeout(
                quote_fn,
                _BATCH_TIMEOUT,
                symbol=[p.yahoo for p in chunk],
                provider="yfinance",
            )
            got = 0
            for item in res.results or []:
                d = item.model_dump()
                p = by_yahoo.get(str(d.get("symbol")))
                if p is None:
                    continue
                q = _quote_from_dump(p, d)
                if q is not None:
                    out[p.yahoo] = q
                    got += 1
            empty_streak = 0 if got else empty_streak + 1
        except Exception:
            empty_streak += 1
        if empty_streak >= _EMPTY_STREAK_GIVEUP:
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


