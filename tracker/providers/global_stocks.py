"""全球股票 provider: 美股/港股/德英加澳 (IBKR 优先 → yfinance → 港股 akshare 兜底).

覆盖市场: 裸代码=美股, .HK=港股, .DE/.F/.BE/.DU/.HM/.MU=德股,
.L/.IL/.AL=英股, .TO/.V/.CN/.NE=加股, .AX=澳股, .SI=新加坡 (SGX, .SG 别名归一)。
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

_AK_TIMEOUT = 6.0
# 按市场独立缓存: 某市场接口失败不影响其它市场, 且失败不占用 TTL (可重试)
_ak_spot_cache: dict[Market, tuple[float, pd.DataFrame]] = {}
_ak_spot_lock = threading.Lock()
_ak_spot_inflight: dict[Market, threading.Lock] = {}  # per-market 并发首拉去重
_AK_SPOT_TTL = 60


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


def _obb():
    from openbb import obb

    return obb


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
    res = with_timeout(
        _obb().equity.price.quote, _QUOTE_TIMEOUT, symbol=p.yahoo, provider="yfinance"
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
    for start in range(0, len(parsed), _YAHOO_CHUNK):
        if start:
            time.sleep(_CHUNK_PAUSE * (2 if empty_streak else 1))
        chunk = parsed[start : start + _YAHOO_CHUNK]
        try:
            # 批量同样要硬超时: OpenBB/yfinance 偶发内部重试会把批量调用挂住,
            # 而它跑在 get_quotes 主线程里 (域间串行), 一挂全批卡死。超时按
            # 空结果处理, 后续逐代码源链还能兜底 (单代码 5s 硬超时)
            res = with_timeout(
                _obb().equity.price.quote,
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
    """
    try:
        import yfinance as yf

        return str(yf.Ticker(yahoo).fast_info.get("currency") or "").strip()
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
        # LSE 惯例: 报价币种为 GBP (价在便士) / GBX / 便士变体 / 未知时 ÷100 折成
        # 英镑; 仅当确证为其它币种 (美元/欧元计价的 LSE ETF/ADR, 如 CSPX.L/VWRA.L)
        # 才不折算 —— 与报价路径 _quote_from_dump 的 is_pence 判据对齐, 避免同一
        # 代码行情与K线单位不一致 (历史曾无条件 ÷100, 把美元计价 LSE 缩错 100 倍)
        # .IL (国际挂盘 IOB) 不适用该惯例, 见下方分支
        ccy = _yahoo_quote_currency(p.yahoo).upper()
        # .IL (LSE 国际挂盘, 如 KAP.IL) 以 USD/EUR 计价: 币种未知时不能按 LSE
        # 主板惯例兜底 ÷100, 仅当确证为便士才折算 (与 _quote_from_dump 判据对齐)
        if p.yahoo.endswith(".IL"):
            pence = is_pence(ccy)
        else:
            pence = ccy in ("", "GBP", "GBX") or is_pence(ccy)
        if pence:
            for c in ("open", "high", "low", "close"):
                if c in df.columns:
                    df[c] = df[c].astype(float) / 100
    return df


# ---------- akshare 源 (港股兜底; A股由 CN provider 专属使用) ----------


def _ak_spot(market: Market) -> pd.DataFrame:
    """全域 spot 快照 (按市场缓存 60s); 并发首拉以 per-key 锁去重, 只发一次请求."""
    global _ak_spot_cache
    now = pd.Timestamp.now().timestamp()
    with _ak_spot_lock:
        hit = _ak_spot_cache.get(market)
        if hit and now - hit[0] < _AK_SPOT_TTL:
            return hit[1]
        gate = _ak_spot_inflight.setdefault(market, threading.Lock())
    acquired = gate.acquire(blocking=False)
    try:
        if acquired:  # 本线程负责拉取; 其它线程等拉取方写回缓存后读结果
            ak = _ak()
            fetcher = {
                Market.CN: ak.stock_zh_a_spot_em,
                Market.BJ: ak.stock_zh_a_spot_em,
                Market.HK: ak.stock_hk_spot_em,
            }.get(market)
            if fetcher is None:
                return pd.DataFrame()
            try:
                df = with_timeout(fetcher, _AK_TIMEOUT)
            except Exception:
                return pd.DataFrame()
            if df.empty:
                return df
            with _ak_spot_lock:
                _ak_spot_cache[market] = (now, df)
            return df
        gate.acquire()
        gate.release()  # 拉取方已 pop 此 gate: 立即释放, 不留持有者
    finally:
        if acquired:
            with _ak_spot_lock:
                if _ak_spot_inflight.get(market) is gate:
                    _ak_spot_inflight.pop(market, None)
            gate.release()
    # 未获 gate: 拉取方已完成, 重读缓存; 缓存仍空 (拉取失败) 则返回空表
    with _ak_spot_lock:
        hit = _ak_spot_cache.get(market)
    if hit and now - hit[0] < _AK_SPOT_TTL:
        return hit[1]
    return pd.DataFrame()


def _akshare_quote(p: ParsedSymbol) -> Quote:
    df = _ak_spot(p.market)
    if df.empty:
        raise RuntimeError(f"akshare spot 不可用 ({p.market.value})")
    code = p.ak_code or ""
    col = "代码" if "代码" in df.columns else df.columns[0]
    row = df[df[col].astype(str) == code]
    if row.empty:
        raise RuntimeError(f"akshare 未找到 {code}")
    r = row.iloc[0]

    def _num(v):
        try:
            f = float(v)
            return f if pd.notna(f) else None
        except (TypeError, ValueError):
            return None

    # 停牌/无成交股东财快照最新价可能为 0 或 NaN: 与其余行情源一致地守卫,
    # 无效即抛错回退 yfinance, 不让 0/NaN 报价污染盈亏/PB/涨跌幅
    price = _num(r["最新价"])
    if price is None or price <= 0:
        raise RuntimeError(f"{p.yahoo}: akshare 无有效最新价 ({r['最新价']!r})")
    prev = r.get("昨收")
    chg = r.get("涨跌幅")

    # A股/北交 spot 自带市净率 (东财实时口径, HK spot 最终列已丢弃): 反推每股净资产,
    # 供 PB 阈值提醒使用 (yfinance BVPS 为滞后财报, 东财口径更准)
    pb = _num(r.get("市净率")) if "市净率" in df.columns else None
    book_value = price / pb if pb is not None and pb > 0 else None
    # spot 同样自带总市值 (本地币种口径), 供市值阈值提醒;
    # 港股 spot 最终列已丢弃该字段, 由长桥/fundamentals 兜底
    market_cap = _num(r.get("总市值")) if "总市值" in df.columns else None
    return Quote(
        symbol=p.yahoo,
        name=str(r.get("名称") or "") or None,
        price=price,
        prev_close=_num(prev),
        change_pct=_num(chg),
        currency=p.currency,
        book_value=book_value,
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
        df = ak.stock_zh_a_hist(**kwargs)
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
        df = ak.stock_hk_hist(**kwargs)
        if df.empty:
            raise RuntimeError(f"{p.yahoo}: akshare HK history 无数据")
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
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    return df[keep]


class GlobalStocksProvider(Provider):
    """美股/港股/全球股票域: IBKR 在批量层前置 (orchestration), 此处 yfinance 为主源,

    港股附 akshare 兜底。A股 (.SS/.SZ/.BJ) 不属于本域。
    新加坡 (.SI) yfinance 主源; IBKR 需 SGX 行情订阅 (contract_spec 默认 SGX)。
    搜索源链: IBKR reqMatchingSymbols (可选) → yfinance Search → 本地目录/代码直查。
    """

    name = "global"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        if p.market is Market.HK:
            # prefer_first=True (--akshare / 国内网络): akshare 在前
            if prefer_first:
                return [_akshare_quote, _yahoo_quote]
            return [_yahoo_quote, _akshare_quote]
        return [_yahoo_quote]

    def history_sources(
        self,
        p: ParsedSymbol,
        start_date: str,
        end_date: str | None,
        prefer_first: bool = False,
    ) -> list:
        if p.market is Market.HK:
            if prefer_first:
                return [
                    lambda: _akshare_history(p, start_date, end_date),
                    lambda: _yahoo_history(p, start_date, end_date),
                ]
            return [
                lambda: _yahoo_history(p, start_date, end_date),
                lambda: _akshare_history(p, start_date, end_date),
            ]
        # 非港股 (美股/德英加澳): 仅 yfinance
        return [lambda: _yahoo_history(p, start_date, end_date)]

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
