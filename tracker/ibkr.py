"""IBKR Gateway 行情接入: ib_async/ib_insync 可选依赖, 失败自动回退.

配置从配置文件读取, 不在代码中写死:
  优先 ibkr.json (真实配置, 已被 gitignore, 可含敏感信息)
  其次 ibkr.example.json (随仓库提供的模板, 兜底保证开箱即用)
  亦可环境变量 IBKR_CONFIG 显式指定路径。

Gateway 模式: "mode" 字段 paper(模拟, 4002) / live(实盘, 4001),
env IBKR_MODE > 文件 mode > 默认 paper; 文件里显式 "port" 优先于模式端口。
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .prices import Quote
from .symbols import Market, ParsedSymbol, is_pence, type_for_symbol

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "ibkr.json"
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "ibkr.example.json"

# 兜底: 无任何配置文件时的最小非敏感连接默认 (IB Gateway 本机默认, 非凭据)
_FALLBACK: dict = {
    "host": "127.0.0.1",
    "mode": "paper",
    "client_id": 17,
    "market_data_type": 3,
    "connect_timeout": 4,
    "exchanges": {},
}

# IB Gateway API 端口: 模拟盘 / 实盘
_MODE_PORTS = {"paper": 4002, "live": 4001}


def _mode_port(cfg: dict) -> int:
    """按 mode 返回 Gateway API 端口; 显式 port 优先 (7496/7497 TWS 仍可用)."""
    explicit = cfg.get("port")
    if explicit is not None:
        return int(explicit)
    mode = str(cfg.get("mode") or "paper").strip().lower()
    return _MODE_PORTS[mode]


_UNAVAILABLE_TTL = 60.0
_client = None
_unavailable: tuple[str, float] | None = None


def _resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("IBKR_CONFIG")
    if env:
        return Path(env)
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    return EXAMPLE_CONFIG


def _load_file(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _merge(base: dict, extra: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in extra.items():
        if k.startswith("_"):
            continue
        if k == "exchanges" and isinstance(v, dict):
            out.setdefault("exchanges", {}).update(v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict:
    """读取 IBKR 配置: 真实文件(或 env/显式 path) 覆盖模板 ibkr.example.json 覆盖兜底默认.

    环境变量 IBKR_MODE (paper/live) 覆盖文件中的 mode; 显式 port 不受影响。
    """
    cfg = _merge(_FALLBACK, _load_file(EXAMPLE_CONFIG))
    target = _resolve_config_path(path)
    if target != EXAMPLE_CONFIG:
        cfg = _merge(cfg, _load_file(target))
    env_mode = os.environ.get("IBKR_MODE")
    if env_mode:
        cfg["mode"] = env_mode.strip().lower()
    return cfg


def reset() -> None:
    global _client, _unavailable
    _client = None
    _unavailable = None


def _ib_module():
    try:
        import ib_async as m

        return m
    except ImportError:
        pass
    try:
        import ib_insync as m

        return m
    except ImportError as e:
        raise RuntimeError("未安装 ib_async (pip install ib_async)") from e


def _get_client(cfg: dict):
    global _client, _unavailable
    if _client is not None and _client.isConnected():
        return _client
    if _unavailable is not None:
        if time.time() - _unavailable[1] < _UNAVAILABLE_TTL:
            return None
        _unavailable = None
    required_keys = ["host", "client_id", "connect_timeout", "market_data_type"]
    for k in required_keys:
        if k not in cfg:
            _unavailable = (f"配置缺失: {k}", time.time())
            return None
    mode = str(cfg.get("mode") or "paper").strip().lower()
    if mode not in _MODE_PORTS:
        _unavailable = (f"未知 mode: {mode!r} (可选: paper/live)", time.time())
        return None
    try:
        m = _ib_module()
        ib = m.IB()
        kwargs = dict(
            clientId=int(cfg["client_id"]),
            timeout=float(cfg["connect_timeout"]),
            readonly=True,
        )
        try:
            kwargs["fetchFields"] = m.StartupFetchNONE
        except AttributeError:
            pass
        ib.connect(
            cfg["host"],
            _mode_port(cfg),
            **kwargs,
        )
        ib.reqMarketDataType(int(cfg["market_data_type"]))
        _client = ib
        return ib
    except Exception as e:
        _unavailable = (f"{type(e).__name__}: {e}"[:200], time.time())
        return None


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    exchange: str
    currency: str
    trading_class: str = ""


def contract_spec(p: ParsedSymbol, exchanges: dict | None = None) -> ContractSpec:
    ex = exchanges if exchanges is not None else load_config().get("exchanges") or {}
    if p.market is Market.US:
        return ContractSpec(p.yahoo, ex.get("US", "SMART"), "USD")
    code, _, suffix = p.yahoo.rpartition(".")
    if p.market in (Market.CN, Market.BJ):
        if p.is_b_share:
            if code.startswith("900"):
                return ContractSpec(code, ex.get("SHSE", "SHSE"), "USD", trading_class=code)
            if code.startswith("200"):
                return ContractSpec(code, ex.get("SZSE", "SZSE"), "HKD", trading_class=code)
        return ContractSpec(code, ex.get("CN", "SEHK"), "CNY", trading_class=code)
    if p.market is Market.HK:
        return ContractSpec(code.lstrip("0") or code, ex.get("HK", "SEHK"), "HKD")
    if p.market is Market.DE:
        return ContractSpec(code, ex.get("DE", "IBIS"), "EUR")
    if p.market is Market.GB:
        return ContractSpec(code, ex.get("GB", "LSE"), "GBP")
    if p.market is Market.CA:
        if suffix == "V":
            return ContractSpec(code, ex.get("CA_V", "TSXV"), "CAD")
        return ContractSpec(code, ex.get("CA", "TSE"), "CAD")
    if p.market is Market.AU:
        return ContractSpec(code, ex.get("AU", "ASX"), "AUD")
    raise ValueError(f"不支持的市场: {p.market}")


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def quote_from_ticker(yahoo_symbol: str, ticker) -> Quote | None:
    price = None
    try:
        price = _f(ticker.marketPrice())
    except Exception:
        price = None
    if price is None or price <= 0:
        price = _f(getattr(ticker, "last", None))
    if price is None or price <= 0:
        price = _f(getattr(ticker, "close", None))
    if price is None or price <= 0:
        return None
    close = _f(getattr(ticker, "close", None))
    contract = getattr(ticker, "contract", None)
    raw_ccy = str(getattr(contract, "currency", "") or "")
    if not raw_ccy:
        return None
    chg = None
    if close and close > 0:
        chg = (price / close - 1) * 100
    if is_pence(raw_ccy):
        currency = "GBP"
        price = price / 100
        if close:
            close = close / 100
    else:
        currency = raw_ccy.upper()
    return Quote(
        symbol=yahoo_symbol,
        name="",
        price=price,
        prev_close=close,
        change_pct=chg,
        currency=currency,
    )


def get_quotes_ibkr(
    parsed: list[ParsedSymbol], cfg: dict | None = None
) -> tuple[dict[str, Quote], str | None]:
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        return {}, reason
    m = _ib_module()
    pairs = []
    for p in parsed:
        spec = contract_spec(p, cfg.get("exchanges"))
        c = m.Contract()
        c.symbol = spec.symbol
        c.secType = "STK"
        c.exchange = spec.exchange
        c.currency = spec.currency
        if spec.trading_class:
            c.tradingClass = spec.trading_class
        pairs.append((p.yahoo, c))
    try:
        qualified = ib.qualifyContracts(*[c for _, c in pairs])
    except Exception as e:
        return {}, f"qualifyContracts 失败: {e}"[:200]
    if not qualified:
        return {}, None
    by_id = {id(c): y for y, c in pairs}
    quotes: dict[str, Quote] = {}
    tickers = []
    try:
        tickers = ib.reqTickers(*qualified)
    except Exception as e:
        return quotes, f"reqTickers 失败: {e}"[:200]
    for t in tickers:
        y = by_id.get(id(t.contract))
        if not y:
            continue
        q = quote_from_ticker(y, t)
        if q:
            quotes[y] = q
    return quotes, None


_A_SHARE_SH_PREFIX = ("5", "6", "9")
_A_SHARE_BJ_PREFIX = ("4", "8")


def ibkr_to_yahoo(
    symbol: str, exchange: str, primary_exchange: str, currency: str
) -> str | None:
    raw = str(symbol).strip()
    sym = raw.replace(" ", "-")
    ex = (exchange or "").upper()
    prim = (primary_exchange or "").upper()
    ccy = (currency or "").upper()
    # SMART/空 等通用路由代号不携带真实交易所信息, 此时以 primaryExchange 为准,
    # 否则 SMART+SHSE 的沪 B 股会误判为美股, SMART+SZSE 的深 B 股会误判为港股
    exkey = prim if ex in ("", "SMART", "BESTEXEC") else ex
    # B 股: 沪 B 以 USD、深 B 以 HKD 计价的 9xxxxx/2xxxxx 六位代码。
    # 交易所/primaryExchange 可能缺失 (SMART + 空), 因此必须先按代码形态判定,
    # 否则沪 B 会被当成美股 (裸代码)、深 B 会被当成港股 (.HK)。
    if len(raw) == 6 and raw.isdigit() and ccy in ("USD", "HKD"):
        if raw.startswith("900"):
            return f"{raw}.SS"
        if raw.startswith("200"):
            return f"{raw}.SZ"
    if ccy == "CNY":
        if raw[:1] in _A_SHARE_BJ_PREFIX or raw.startswith("920"):
            return f"{raw}.BJ"
        if raw[:1] in _A_SHARE_SH_PREFIX:
            return f"{raw}.SS"
        return f"{raw}.SZ"
    if ccy == "HKD" or exkey in ("SEHK", "HKEX"):
        if exkey == "SZSE":
            return f"{sym}.SZ"
        code = raw.lstrip("0") or "0"
        return f"{code.zfill(4)}.HK"
    if ccy == "EUR" or exkey in ("IBIS", "IBISX", "FWB", "GETX", "SWB"):
        return f"{sym}.DE"
    if ccy in ("GBP", "GBX") or exkey in ("LSE", "LSEETF"):
        return f"{sym}.L"
    # 加拿大: Yahoo 后缀按交易所区分 (CSE=.CN, Cboe Canada/NEO=.NE, TSXV=.V, 其余=.TO);
    # 必须在 ccy == "CAD" 兜底之前判定, 否则所有加元持仓都会被当成 TSX
    if exkey in ("CSE",):
        return f"{sym}.CN"
    if exkey in ("NEO", "NEOEX"):
        return f"{sym}.NE"
    if ccy == "CAD" or exkey in ("TSE", "TSXV", "TSX", "CDGX"):
        return f"{sym}.V" if exkey == "TSXV" else f"{sym}.TO"
    if ccy == "AUD" or exkey == "ASX":
        return f"{sym}.AX"
    if ccy == "USD" or exkey in (
        "SMART", "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "ISLAND", "PSX", "DRCTEDGE",
    ):
        if exkey == "SHSE":
            return f"{sym}.SS"
        return sym
    return None


def get_history_ibkr(
    parsed: list[ParsedSymbol], months: int, cfg: dict | None = None
) -> tuple[dict[str, pd.DataFrame], str | None]:
    import pandas as pd

    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        return {}, reason
    m = _ib_module()
    results: dict[str, pd.DataFrame] = {}
    for p in parsed:
        spec = contract_spec(p, cfg.get("exchanges"))
        c = m.Contract()
        c.symbol = spec.symbol
        c.secType = "STK"
        c.exchange = spec.exchange
        c.currency = spec.currency
        if spec.trading_class:
            c.tradingClass = spec.trading_class
        try:
            qualified = ib.qualifyContracts(c)
            if not qualified:
                continue
            bars = ib.reqHistoricalData(
                qualified[0],
                endDateTime="",
                durationStr=f"{months} M",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
            if not bars:
                continue
            rows = []
            for b in bars:
                rows.append(
                    {
                        "date": b.date,
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": getattr(b, "volume", 0) or 0,
                    }
                )
            df = pd.DataFrame(rows)
            if not df.empty:
                results[p.yahoo] = df
        except Exception:
            continue
    return results, None


def get_fx_rate_ibkr(
    src: str, dst: str, cfg: dict | None = None
) -> tuple[float | None, str | None]:
    if src == dst:
        return 1.0, None
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        return None, reason
    m = _ib_module()
    c = m.Contract()
    c.symbol = src.upper()
    c.secType = "CASH"
    c.exchange = "IDEALPRO"
    c.currency = dst.upper()
    try:
        qualified = ib.qualifyContracts(c)
        if not qualified:
            return None, "无法 qualify FX contract"
        tickers = ib.reqTickers(qualified[0])
        if not tickers:
            return None, "无 FX ticker 数据"
        t = tickers[0]
        price = None
        try:
            price = _f(t.marketPrice())
        except Exception:
            pass
        if price is None or price <= 0:
            price = _f(getattr(t, "last", None))
        if price is None or price <= 0:
            price = _f(getattr(t, "close", None))
        if price is None or price <= 0:
            return None, "FX 价格无效"
        return price, None
    except Exception as e:
        return None, f"FX 获取失败: {e}"


def fetch_positions(cfg: dict | None = None) -> list:
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        raise RuntimeError(f"IBKR 不可用: {reason}")
    return [pos for pos in ib.positions() if getattr(pos.contract, "secType", "") == "STK"]


def positions_to_rows(positions) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    skipped: list[str] = []
    for pos in positions:
        c = getattr(pos, "contract", None)
        qty = float(getattr(pos, "position", 0) or 0)
        if qty == 0:
            continue
        raw = str(getattr(c, "symbol", "")).strip()
        y = ibkr_to_yahoo(
            raw,
            getattr(c, "exchange", ""),
            getattr(c, "primaryExchange", ""),
            getattr(c, "currency", ""),
        )
        if not y:
            skipped.append(
                f"{raw} ({getattr(c, 'exchange', '')}/{getattr(c, 'currency', '')})"
                " 无法映射为 Yahoo 代码"
            )
            continue
        rows.append(
            {
                "symbol": y,
                "type": type_for_symbol(y),
                "quantity": qty,
                "avg_cost": round(float(getattr(pos, "avgCost", 0) or 0), 6),
            }
        )
    return rows, skipped
