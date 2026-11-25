"""全球股票 provider: 美股/港股/德荷英加澳/日韩 (IBKR 优先 → EODHD 可选 → yfinance → 港股 akshare 兜底).

覆盖市场: 裸代码=美股, .HK=港股, .DE/.F/.BE/.DU/.HM/.MU=德股,
.AS=荷兰股 (Euronext Amsterdam), .L/.IL/.AL=英股, .TO/.V/.CN/.NE=加股,
.AX=澳股, .SI=新加坡 (SGX, .SG 别名归一), .T=日股 (东京, .JP 别名归一),
.KS/.KQ=韩股 (KOSPI/KOSDAQ)。
"""

from __future__ import annotations

from ..deps import yfinance as _yf
from ..symbols import Market, ParsedSymbol
from .base import Provider, SymbolEntry, ordered_sources
from .em_suggest import parse_symbol_safe
from .stock_akshare import (  # noqa: F401 - 兼容再导出
    _AK_FAIL_TTL,
    _AK_HIST_TIMEOUT,
    _ak_quote_raw,
    _ak_secid,
    _akshare_history,
    _akshare_quote,
    _sina_quote,
)
from .stock_yahoo import (  # noqa: F401 - 兼容再导出
    _quote_from_dump,
    _yahoo_batch,
    _yahoo_history,
    _yahoo_quote,
    _yahoo_quote_currency,
    quote_throttled,
)


def _market_label(yahoo: str) -> str:
    """规范代码 → 市场标签 (港股/美股/德股/荷兰股…); 供搜索结果展示。"""
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
        s = _yf().Search(query.strip(), max_results=10, news_count=0, timeout=6)
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
        from .. import source_order as so

        cfg = eodhd_mod.load_config()
        eod = eodhd_mod.available(cfg)
        mode = cfg["quotes"] if eod else "off"
        order = so.display_order(
            "global",
            "quote",
            prefer_akshare=bool(prefer_first),
            eodhd_quotes=mode,
            eodhd_priority=cfg["priority"],
        )
        funcs = {}
        if not quote_throttled():
            funcs["yfinance"] = _yahoo_quote
        if p.market is Market.HK:
            funcs["akshare_em"] = _akshare_quote
            funcs["akshare_sina"] = _sina_quote
        if eod and mode != "off":
            funcs["eodhd"] = eodhd_mod.quote
        return ordered_sources(order, funcs)

    def history_sources(
        self,
        p: ParsedSymbol,
        start_date: str,
        end_date: str | None,
        prefer_first: bool = False,
    ) -> list:
        from .. import eodhd as eodhd_mod
        from .. import source_order as so

        cfg = eodhd_mod.load_config()
        eod = eodhd_mod.available(cfg)
        eod_src = lambda: eodhd_mod.history(p, start_date, end_date)  # noqa: E731
        yahoo_src = lambda: _yahoo_history(p, start_date, end_date)  # noqa: E731
        ak_src = lambda: _akshare_history(p, start_date, end_date)  # noqa: E731

        order = so.display_order(
            "global",
            "history",
            prefer_akshare=bool(prefer_first),
            eodhd_quotes=cfg["quotes"],
            eodhd_priority=cfg["priority"],
        )
        funcs = {"yfinance": yahoo_src}
        if eod:
            funcs["eodhd"] = eod_src
        if p.market is Market.HK:
            funcs["akshare_em"] = ak_src
        return ordered_sources(order, funcs)

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
