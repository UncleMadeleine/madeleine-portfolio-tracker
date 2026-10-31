"""全球股票 provider: 美股/港股/德英加澳/日韩 (IBKR 优先 → EODHD 可选 → yfinance → 港股 akshare 兜底).

覆盖市场: 裸代码=美股, .HK=港股, .DE/.F/.BE/.DU/.HM/.MU=德股,
.L/.IL/.AL=英股, .TO/.V/.CN/.NE=加股, .AX=澳股, .SI=新加坡 (SGX, .SG 别名归一),
.T=日股 (东京, .JP 别名归一), .KS/.KQ=韩股 (KOSPI/KOSDAQ)。
"""

from __future__ import annotations

import math
import threading
import time

import pandas as pd

from ..symbols import Market, ParsedSymbol, is_pence
from ..util import with_timeout
from .base import Provider, Quote, SymbolEntry
from .em_suggest import parse_symbol_safe

# akshare 单代码日K (stock_zh_a_hist / stock_hk_hist): 接口不传 timeout,
# 给硬超时防挂死 (单代码请求通常 1-3s, 30s 覆盖慢网/重试)
_AK_HIST_TIMEOUT = 30.0
# 单代码实时行情失败占位 TTL: 东财不可达/限流时不反复打 (与全市场 spot 的
# 失败占位同思路, 现在是 per-symbol 粒度)
_AK_FAIL_TTL = 15.0


def _market_label(yahoo: str) -> str:
    """规范代码 → 市场标签 (港股/美股/德股…); 供搜索结果展示。"""
    from ..symbols import MARKET_META, parse

    try:
        return MARKET_META[parse(yahoo).market]["label"]
    except Exception:
        return "美股"


def _yf_search(query: str) -> list[dict]:
    """yfinance Search 封装; 失败返回 [] (调用方降级下一源)。

    仅保留股票形态 (EQUITY/ETF); 加密/期货/基金等属其他域或不可交易。
    返回 [{symbol, name}]。
    """
    try:
        import yfinance as yf

        s = yf.Search(query.strip(), max_results=10, news_count=0, timeout=6)
        quotes = s.quotes or []
    except Exception:
        return []
    out: list[dict] = []
    for row in quotes:
        if row.get("quoteType") not in ("EQUITY", "ETF"):
            continue
        sym = str(row.get("symbol") or "").strip()
        name = str(row.get("shortname") or row.get("longname") or "").strip()
        if sym and name:
            out.append({"symbol": sym, "name": name})
    return out


_obb_singleton = None
_obb_lock = threading.Lock()


def _obb():
    """OpenBB 单例 (线程安全缓存).

    首次 `from openbb import obb` 有冷启动开销 (本机实测 5.5s), 之后是进程内
    单例。价值在于让批量/历史/汇率/crypto 路径共用一次初始化, 而不是省下单次
    调用的时间。

    注意: 冷启动并不吃掉 _QUOTE_TIMEOUT 的预算。`with_timeout(fn, t, *args)`
    的实参在调用它之前就已在主线程求值完毕, daemon 线程只执行已解析的可调用
    对象, 所以 `_obb()` 无论写在哪里都在计时区外。曾以为「把 _obb() 移出
    with_timeout 就能修好首次超时」——那是错的, 那样改动语义上无效。

    真正的超时风险是首次 quote 本身慢 (实测 4.5s, 阈值 5.0s, 余量仅 0.5s),
    属临界竞态; 单例化缩小了它, 没有消除它。

    失败行为与原来一致: openbb 未安装/导入异常时 import 语句本身抛错,
    单例保持 None, 下次调用重试 (不缓存失败)。
    """
    global _obb_singleton
    if _obb_singleton is None:
        with _obb_lock:
            if _obb_singleton is None:
                from openbb import obb

                _obb_singleton = obb
    return _obb_singleton


def _ak():
    import akshare as ak

    return ak


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
    if raw_ccy is None:
        # .IL (LSE 国际挂盘) 证券以 USD/EUR 计价 (KAP.IL=USD): 币种缺失时不能按
        # LSE 主板惯例兜底 GBp, 便士兜底会把价格再缩小 100 倍
        if p.yahoo.endswith(".IL"):
            raw_ccy = "USD"
        elif p.market is Market.GB:
            raw_ccy = "GBp"
        else:
            raw_ccy = p.currency
    raw_ccy = str(raw_ccy).strip()
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


def _yahoo_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    kwargs = {"symbol": p.yahoo, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    res = _obb().equity.price.historical(**kwargs)
    df = res.to_dataframe().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df = df.dropna(subset=["close"])
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    df = df[keep]
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
#              f116=总市值 f167=市净率 f170=涨跌幅(%)
_AK_QUOTE_FIELDS = "f43,f57,f58,f60,f116,f167,f170"

# 单代码行情进程内缓存 (短 TTL): 同一代码紧接着的重复请求 (UI 渲染 + 后台
# 刷新) 只发一次; per-key 单飞去重并发首拉; 失败写短 TTL 占位防反复打东财
_AK_QUOTE_TTL = 5.0
_ak_quote_cache: dict[str, tuple[float, dict | None]] = {}
_ak_quote_lock = threading.Lock()
_ak_quote_inflight: dict[str, threading.Lock] = {}


def _requests():
    import requests

    return requests


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
    return Quote(
        symbol=p.yahoo,
        name=str(d.get("f58") or "").strip() or None,
        price=price,
        prev_close=_ak_num(d.get("f60")),
        change_pct=_ak_num(d.get("f170")),
        currency=p.currency,
        book_value=price / pb if pb is not None and pb > 0 else None,
        market_cap=market_cap if market_cap is not None and market_cap > 0 else None,
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


class GlobalStocksProvider(Provider):
    """美股/港股/全球股票域: IBKR 在批量层前置 (orchestration), 此处 yfinance 为主源,

    港股附 akshare 兜底。A股 (.SS/.SZ/.BJ) 不属于本域。
    新加坡 (.SI) / 日股 (.T) / 韩股 (.KS/.KQ) 均 yfinance 主源;
    日韩另可走 IBKR (contract_spec 默认 TSEJ/KRX/KOSDAQ), 长桥不支持。
    搜索源链: IBKR reqMatchingSymbols (可选) → yfinance Search → 本地目录/代码直查。
    """

    name = "global"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        from .. import eodhd as eodhd_mod

        cfg = eodhd_mod.load_config()
        eod = eodhd_mod.available(cfg)
        mode = cfg["quotes"] if eod else "off"
        if p.market is Market.HK:
            # prefer_first=True (--akshare / 国内网络): akshare 在前
            base = (
                [_akshare_quote, _yahoo_quote]
                if prefer_first
                else [_yahoo_quote, _akshare_quote]
            )
        else:
            base = [_yahoo_quote]
        if mode == "primary":
            # 显式要求 akshare 优先时不抢跑: akshare → EODHD → yahoo
            if p.market is Market.HK and prefer_first:
                return [_akshare_quote, eodhd_mod.quote, _yahoo_quote]
            return [eodhd_mod.quote, *base]
        if mode == "fallback":
            return [*base, eodhd_mod.quote]
        return base

    def history_sources(
        self,
        p: ParsedSymbol,
        start_date: str,
        end_date: str | None,
        prefer_first: bool = False,
    ) -> list:
        from .. import eodhd as eodhd_mod

        cfg = eodhd_mod.load_config()
        eod = eodhd_mod.available(cfg)
        primary = eod and cfg["priority"] == "primary"
        eod_src = lambda: eodhd_mod.history(p, start_date, end_date)  # noqa: E731
        yahoo_src = lambda: _yahoo_history(p, start_date, end_date)  # noqa: E731
        ak_src = lambda: _akshare_history(p, start_date, end_date)  # noqa: E731
        if p.market is Market.HK:
            if prefer_first:
                chain = [ak_src, yahoo_src]
                return (
                    [ak_src, eod_src, yahoo_src]
                    if primary
                    else chain + ([eod_src] if eod else [])
                )
            chain = [yahoo_src, ak_src]
            return (
                [eod_src, *chain] if primary else chain + ([eod_src] if eod else [])
            )
        # 非港股 (美股/德英加澳): yfinance 为主, EODHD 可选前置/兜底
        if primary:
            return [eod_src, yahoo_src]
        return [yahoo_src] + ([eod_src] if eod else [])

    # -- 搜索: IBKR (可选) → 东财 suggest (纯数字, 补零命中港股) → yfinance Search → 合法代码直查 --

    def search(self, query: str, limit: int = 10) -> list[SymbolEntry]:
        """全球域搜索: IBKR → 东财 suggest (纯数字/非 ASCII) → yf → 代码直查。

        东财 suggest 用于两类查询 (含补零二次请求, 700 → 00700 命中
        腾讯控股 0700.HK): 纯数字 (港股 4-5 位代码) 与非 ASCII (中文命中
        港股/中概英文名, yf 对中文无召回)。其余查询 (ASCII) 不走东财 ——
        英文名/代码召回由 IBKR/yf/直查覆盖。
        """
        from ..ibkr import ibkr_to_yahoo, search_matches
        from ..search import _fallback_match

        q = query.strip()
        if not q:
            return []

        out: list[SymbolEntry] = []
        seen: set[str] = set()

        def _add(yahoo: str, name: str, market: str) -> None:
            p = parse_symbol_safe(yahoo)
            if p is None or p.yahoo in seen or p.type != "global":
                return
            seen.add(p.yahoo)
            out.append(SymbolEntry(p.yahoo, name or p.market_label, market, p.type))

        # 源 1: IBKR 合约模糊匹配 (Gateway 在线时; 英文名/代码召回最好)
        ibkr_rows = search_matches(q)
        if ibkr_rows:
            for row in ibkr_rows[: limit * 2]:
                yahoo = ibkr_to_yahoo(
                    row["symbol"],
                    row["exchange"],
                    row["primary_exchange"],
                    row["currency"],
                )
                if yahoo:
                    _add(yahoo, row["long_name"] or row["symbol"], _market_label(yahoo))

        # 源 2: 东财 suggest (纯数字 / 非 ASCII 查询): 700 补零命中 00700 腾讯控股,
        # 中文名命中港股/中概 (东财 suggest 支持中文, yf 不支持); 只取港股行
        if q.isdigit() or not q.isascii():
            from .em_suggest import (
                em_code_to_yahoo,
                normalize_suggest_row,
                suggest_merged,
            )

            merged = suggest_merged(q)
            if merged is not None:
                rows, _failed = merged
                for row in rows:
                    norm = normalize_suggest_row(row)
                    if norm is None or norm[1] != "港股":
                        continue
                    yahoo = em_code_to_yahoo(*norm)
                    _add(yahoo, row.get("Name") or yahoo, _market_label(yahoo))
                    if len(out) >= limit:
                        break

        # 源 3: yfinance Search (中文名不支持, 英文名/代码; 本域行情主源同一家)
        if len(out) < limit:
            for row in _yf_search(q):
                _add(row["symbol"], row["name"], _market_label(row["symbol"]))

        # 源 4: 合法代码直查 (SAP.DE / BP.L 等带后缀代码; 非 ASCII 不认领;
        # 裸纯数字不认领 —— 纯数字在美股域无规范形态, 上面已按港股处理)
        if not out:
            if q.isascii() and not q.isdigit():
                fb = _fallback_match(q, limit)
                return [
                    SymbolEntry(r["code"], r["name"], r["market"], r["type"])
                    for r in fb
                    if r["type"] == "global"
                ]
        return out[:limit]
