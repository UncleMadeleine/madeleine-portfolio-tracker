"""IBKR Gateway 行情接入: ib_async/ib_insync 可选依赖, 失败自动回退.

配置从配置文件读取, 不在代码中写死:
  优先 ibkr.json (数据目录下的真实配置, 已被 gitignore, 可含敏感信息)
  其次 ibkr.example.json (包内模板 tracker/templates/, 兜底保证开箱即用)
  亦可环境变量 IBKR_CONFIG 显式指定路径。
  数据目录 = TRACKER_HOME > 源码树仓库根 > 平台用户目录 (见 tracker/paths.py);
  pip 安装后首次使用 `tracker init` 可把模板复制到数据目录再改。

Gateway 模式: "mode" 字段 paper(模拟, 4002) / live(实盘, 4001),
env IBKR_MODE > 文件 mode > 默认 paper; 文件里显式 "port" 优先于模式端口。
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .paths import TEMPLATES_DIR, data_home
from .prices import Quote
from .symbols import Market, ParsedSymbol, is_pence, type_for_symbol

# 用户真实配置落数据目录 (源码树即仓库根; pip 安装后 ~/.tracker/ibkr.json);
# 模板随包分发, env IBKR_CONFIG 优先于两者
DEFAULT_CONFIG = data_home() / "ibkr.json"
EXAMPLE_CONFIG = TEMPLATES_DIR / "ibkr.example.json"

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

# IBKR 批量分块: 账户并发行情线有限额 (典型 100), 一次性 reqTickers 全部
# 合约 (自选 600+ 时) 超量的请求会被 IBKR 静默忽略 → ib_async 的 gather
# 永远等不到 future (RequestTimeout 默认 0 = 无超时) → 整页挂死; 未订阅的
# 合约则让 gather 抛错、整批结果全丢。按块发送 + 块内硬超时 + 空块预算兜底。
#
# 实测成本模型 (market_data_type=3 延迟行情, 本机 Gateway): reqTickers 有
# ~14s/次 的固定快照周期成本 (与块内合约数几乎无关, 50→14s, 150→18s),
# qualify ~0.04s/合约; 块越大越划算, 但受账户并发行情线限额约束。单合约
# 错误 (如 Error 354 未订阅) 只丢该合约, 不污染同块其它合约 (已实测)。
_IBKR_CHUNK = 100  # 每块合约数 (守住典型 100 并发限额; 大余额账户可调大)
_IBKR_CHUNK_TIMEOUT = 22.0  # 单块 (qualify+reqTickers) 硬超时 (s)
_IBKR_WASTED_BUDGET = 30.0  # 空块累计耗时预算 (s): 超过则放弃剩余块
_IBKR_EMPTY_GIVEUP = 2  # 连续空块 (无订阅/无数据): 放弃剩余块
# 连上但零行情 (典型: 账户无所查市场的行情订阅): TTL 内跳过整个 IBKR 阶段,
# 否则每次冷启动都要白等块超时后才落默认源
_IBKR_NO_DATA_TTL = 300.0
_no_data_until: float = 0.0


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
    global _client, _unavailable, _no_data_until
    _client = None
    _unavailable = None
    _no_data_until = 0.0


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
        # ib_async 默认 RequestTimeout=0 (无超时): reqTickers 对不返回的合约
        # 会永久挂起并拖死页面。统一给所有阻塞请求上硬超时。
        ib.RequestTimeout = _IBKR_CHUNK_TIMEOUT
        ib.reqMarketDataType(int(cfg["market_data_type"]))
        _client = ib
        return ib
    except Exception as e:
        _unavailable = (_conn_fail_reason(e, cfg), time.time())
        return None


def _conn_fail_reason(err: Exception, cfg: dict) -> str:
    """连接类失败附带排障提示: Gateway 是否运行 / 端口与 mode 是否匹配."""
    ename = type(err).__name__
    detail = str(err).strip()
    msg = f"{ename}: {detail}"[:160] if detail else ename
    text = detail.lower()
    if (
        "connectionrefuse" in ename.lower()
        or "timeout" in ename.lower()
        or "timeout" in text
        or "connect call failed" in text
    ):
        port = _mode_port(cfg)
        mode = str(cfg.get("mode") or "paper").strip().lower()
        msg += (
            f" — 请检查 IB Gateway/TWS 是否已运行且 API 已启用: "
            f"当前 mode={mode} → 端口 {port} (live=4001 / paper=4002 / "
            f"TWS=7496/7497; ibkr.json 可显式写 port)"
        )
    return msg[:400]


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    exchange: str
    currency: str
    trading_class: str = ""


def contract_spec(p: ParsedSymbol, exchanges: dict | None = None) -> ContractSpec:
    ex = exchanges if exchanges is not None else load_config().get("exchanges") or {}
    if p.market is Market.US:
        # 美股类别股 Yahoo 用连字符 (BRK-B), IBKR 合约库用空格 (BRK B):
        # 连字符直传 qualifyContracts 会查无合约
        symbol = p.yahoo.replace("-", " ")
        return ContractSpec(symbol, ex.get("US", "SMART"), "USD")
    code, _, suffix = p.yahoo.rpartition(".")
    if p.market in (Market.CN, Market.BJ):
        if p.is_b_share:
            if code.startswith("900"):
                return ContractSpec(
                    code, ex.get("SHSE", "SHSE"), "USD", trading_class=code
                )
            if code.startswith("200"):
                return ContractSpec(
                    code, ex.get("SZSE", "SZSE"), "HKD", trading_class=code
                )
        # A股经沪深港通在 IBKR 交易: 沪=SEHKNTL, 深=SEHKSZSE, 科创板=SEHKSTAR;
        # 计价为 CNH (离岸人民币), CNY/SSE/SZSE/SMART 均不被 IBKR 合约库识别
        if p.market is Market.BJ:
            # 北交所无沪深港通通道: IBKR 不提供, 落 SEHKNTL 会查无合约,
            # 快速失败让调用方回退 akshare/yfinance
            return ContractSpec(code, ex.get("BJ", "SEHKNTL"), "CNH")
        # 科创板 (.SS 68 开头) 走 SEHKSTAR, 其余沪股走 SEHKNTL
        if suffix == "SS" and code.startswith("68"):
            return ContractSpec(code, ex.get("CN_STAR", "SEHKSTAR"), "CNH")
        if suffix == "SS":
            return ContractSpec(code, ex.get("CN_SH", "SEHKNTL"), "CNH")
        return ContractSpec(code, ex.get("CN_SZ", "SEHKSZSE"), "CNH")
    if p.market is Market.HK:
        # 港股是多柜台 (HKEX Multi-Counter): 同一标的有 HKD 主柜台 / RMB 柜台
        # (8 开头 5 位, 如 82318.HK) / USD 柜台 (如 9804.HK PP越南-U) 三个独立代码,
        # 分别以 HKD/CNH/USD 计价, 且 Yahoo 代码形态上无法区分 (9988.HK 是 HKD,
        # 9804.HK 是 USD)。硬编码 currency=HKD 会让后两种柜台 qualify 查无合约
        # (Error 200) 而整只回退 yfinance。留空 currency 让 IBKR 按代码自行补全
        # (每个柜台代码在 SEHK 唯一, qualify 必得唯一合约), 真实币种由返回的
        # ticker.contract.currency 读回 (见 quote_from_ticker)。
        return ContractSpec(code.lstrip("0") or code, ex.get("HK", "SEHK"), "")
    if p.market is Market.DE:
        return ContractSpec(code, ex.get("DE", "IBIS"), "EUR")
    if p.market is Market.GB:
        # LSE 合约 symbol 带尾点 (BP./HSBA./VOD.); 裸代码 qualify 查无合约
        return ContractSpec(code + ".", ex.get("GB", "LSE"), "GBP")
    if p.market is Market.CA:
        # 加拿大三所: TSX(TSE) / TSXV(TSXV) / CSE(CSE) / NEO(Cboe Canada, NEOE);
        # .CN/.NE 误入 TSE 会让 qualifyContracts 报无安全定义
        if suffix == "V":
            return ContractSpec(code, ex.get("CA_V", "TSXV"), "CAD")
        if suffix == "CN":
            return ContractSpec(code, ex.get("CA_CN", "CSE"), "CAD")
        if suffix == "NE":
            return ContractSpec(code, ex.get("CA_NE", "NEOE"), "CAD")
        return ContractSpec(code, ex.get("CA", "TSE"), "CAD")
    if p.market is Market.AU:
        return ContractSpec(code, ex.get("AU", "ASX"), "AUD")
    if p.market is Market.SG:
        # SGX 同时存在 SGD 计价 (D05.SI 星展 / C6L.SI 新航) 与 USD 计价 (D01.SI /
        # NIO.SI 等) 的证券, Yahoo 的 .SI 后缀不带币种信息。硬编码 SGD 会让 USD
        # 证券 qualify 查无合约 (Error 200) 而回退 yfinance; 留空 currency 由 IBKR
        # 按代码补全, 真实币种从 ticker.contract.currency 读回。
        return ContractSpec(code, ex.get("SG", "SGX"), "")
    if p.market is Market.BR:
        # B3 (圣保罗): Yahoo 后缀 .SA (PETR4.SA); IBKR 合约库交易代码无后缀数字。
        # 交易所代号现行为 "B3" (旧代号 BVMF 已下线, 直传会报「交易所无效」)
        return ContractSpec(code, ex.get("BR", "B3"), "BRL")
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
    # 港股人民币柜台经 IBKR 报 CNH (离岸人民币), 与本项目 CNY 口径统一 (与长桥
    # _currency_for 一致): 否则汇率层只认 CNY, CNH 会落 missing 无法折算。
    if currency == "CNH":
        currency = "CNY"
    return Quote(
        symbol=yahoo_symbol,
        name="",
        price=price,
        prev_close=close,
        change_pct=chg,
        currency=currency,
        source="ibkr",
    )


def _claims_global(p: ParsedSymbol) -> bool:
    """IBKR 认领范围: 全球域 (美股/港股/德英加澳新).

    A股/B股/北交所始终走 akshare→yfinance (CNY 实时价, 分域记账规范);
    crypto/index 无 IBKR 合约。不认领的交回调用方默认链。
    """
    return p.market not in (
        Market.CN,
        Market.BJ,
        Market.CRYPTO,
        Market.INDEX,
    )


def get_quotes_ibkr(
    parsed: list[ParsedSymbol], cfg: dict | None = None
) -> tuple[dict[str, Quote], str | None]:
    # 认领过滤先于连接: 全部不认领 (纯 A股/crypto 批) 时不建 Gateway 会话
    claimed = [p for p in parsed if _claims_global(p)]
    if not claimed:
        return {}, None
    global _no_data_until
    # 刚检测过「连上但零行情」(无订阅): TTL 内跳过, 不重复白等块超时
    if time.time() < _no_data_until:
        return {}, "暂无行情数据 (刚检测过, 稍后自动重试), 已回退默认数据源"
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        return {}, reason
    m = _ib_module()
    quotes: dict[str, Quote] = {}
    empty_streak = 0
    wasted = 0.0  # 空块累计耗时 (有数据的块不计): 无订阅/超限额时及时收手
    for start in range(0, len(claimed), _IBKR_CHUNK):
        if start:
            time.sleep(0.3)  # 块间轻微退让, 避免打满 IBKR 消息频率
        t_chunk = time.time()
        got = _ibkr_chunk_quotes(ib, m, claimed[start : start + _IBKR_CHUNK], cfg)
        quotes.update(got)
        if got:
            empty_streak = 0
            wasted = 0.0
        else:
            empty_streak += 1
            wasted += time.time() - t_chunk
            if empty_streak >= _IBKR_EMPTY_GIVEUP or wasted > _IBKR_WASTED_BUDGET:
                break  # 连续空块/空块耗时超预算: 剩余合约交回默认数据源
    if not quotes:
        # 全批零行情: 负缓存, 之后 5 分钟的加载直接跳过 IBKR 阶段
        _no_data_until = time.time() + _IBKR_NO_DATA_TTL
    return quotes, None


def _ibkr_chunk_quotes(ib, m, chunk: list[ParsedSymbol], cfg: dict) -> dict[str, Quote]:
    """一块合约的行情: qualify → reqTickers → 提价。

    块内硬超时由 ib.RequestTimeout 保证 (连接时统一设置); 单块失败
    (超时/IBKR 报错) 返回 {} 由调用方计数, 不中断其余块, 也不抛到页面。
    """
    pairs = []
    for p in chunk:
        spec = contract_spec(p, cfg.get("exchanges"))
        c = m.Contract()
        c.symbol = spec.symbol
        c.secType = "STK"
        c.exchange = spec.exchange
        c.currency = spec.currency
        if spec.trading_class:
            c.tradingClass = spec.trading_class
        pairs.append((p.yahoo, c))
    # qualifyContracts 对查无定义的合约会在返回列表塞 None (ib_async 行为):
    # 一只坏合约若混入 reqTickers 会炸整批 ('NoneType' has no attribute 'secType'),
    # 因此按位过滤, 只保留 qualify 成功的合约
    try:
        qualified = ib.qualifyContracts(*[c for _, c in pairs])
    except Exception:
        return {}
    good = [(c, y) for c, y in zip(qualified, [yy for yy, _ in pairs]) if c is not None]
    if not good:
        return {}
    try:
        tickers = ib.reqTickers(*[c for c, _ in good])
    except Exception:
        return {}
    by_id = {id(c): y for c, y in good}
    out: dict[str, Quote] = {}
    for t in tickers:
        y = by_id.get(id(t.contract))
        if not y:
            continue
        q = quote_from_ticker(y, t)
        if q:
            out[y] = q
    return out


_A_SHARE_SH_PREFIX = ("5", "6", "9")
_A_SHARE_BJ_PREFIX = ("4", "8")


def search_matches(pattern: str, cfg: dict | None = None) -> list[dict] | None:
    """IBKR 合约模糊搜索 (reqMatchingSymbols); 返回归一化行, 不可用返回 None.

    行: {symbol, exchange, primary_exchange, currency, long_name}。
    仅本机 Gateway 在线时可用; 调用方 (global provider search) 据此降级。
    """
    pat = (pattern or "").strip()
    if not pat or len(pat) < 2 or not pat.isascii():
        return []  # IBKR 只匹配 ASCII 代码/名称片段; 空参数直接无结果
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        return None
    try:
        descs = ib.reqMatchingSymbols(pat)
    except Exception:
        return None
    out: list[dict] = []
    for d in descs or []:
        c = getattr(d, "contract", None)
        if c is None or getattr(c, "secType", "") != "STK":
            continue
        out.append(
            {
                "symbol": str(getattr(c, "symbol", "") or "").strip(),
                "exchange": str(getattr(c, "exchange", "") or "").strip(),
                "primary_exchange": str(
                    getattr(c, "primaryExchange", "") or ""
                ).strip(),
                "currency": str(getattr(c, "currency", "") or "").strip(),
                "long_name": str(getattr(c, "longName", "") or "").strip(),
            }
        )
    return out


def ibkr_to_yahoo(
    symbol: str, exchange: str, primary_exchange: str, currency: str
) -> str | None:
    raw = str(symbol).strip()
    # LSE 合约 symbol 带尾点 (BP./HSBA./VOD., 本文件 contract_spec 依赖这一点):
    # 不剥掉会拼出 "BP..L" (parse 不报错, 之后永远取不到行情)
    sym = raw.replace(" ", "-").rstrip(".")
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
    if ccy in ("CNY", "CNH"):
        # 沪深港通 A 股经 IBKR 交易挂 SEHK 通道但报 CNY/CNH (见 contract_spec
        # 用 CNH 请求); 港股人民币柜台 (HKEX RMB counter, 8 开头 5 位如 82318)
        # 也以 CNY 交易。CNH (离岸人民币) 与 CNY 都要认, 否则 A股持仓全落 skipped。
        # 两者都要先于代码前缀判定: 柜台 82318+SEHK 的 8 恰在北交所前缀里,
        # 误判会落成 82318.BJ。通道与柜台以「交易所 + 代码形态」区分:
        # 沪深港通代码 6 位, 柜台 lstrip 后 ≤5 位且 8 开头 (与北交所 830/430/920 不重叠)。
        if exkey in ("SEHK", "HKEX") and len(raw) == 5 and raw.isdigit() and raw.startswith("8"):
            code = raw.lstrip("0") or "0"
            return f"{code.zfill(4)}.HK"
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
    # 仅德国交易所 (IBIS/Xetra, FWB/法兰克福, GETX, SWB/斯图加特等) 映射 .DE;
    # 不再用裸 ccy=="EUR" 兜底 —— 那会把泛欧所 (SBF/AEB/EBR/MTA) 也错映射到
    # 一个错误或不存在的德国代码, 本项目只支持德股, 其余欧元所应落 None 进 skipped
    if exkey in ("IBIS", "IBISX", "FWB", "GETX", "GETXEX", "SWB"):
        return f"{sym}.DE"
    if ccy in ("GBP", "GBX") or exkey in ("LSE", "LSEETF"):
        return f"{sym}.L"
    # 加拿大: Yahoo 后缀按交易所区分 (CSE=.CN, Cboe Canada/NEO=.NE, TSXV=.V, 其余=.TO);
    # 必须在 ccy == "CAD" 兜底之前判定, 否则所有加元持仓都会被当成 TSX
    # CXI = IBKR 部分行情链路对 CSE 的别名代号
    if exkey in ("CSE", "CXI", "XCNQ"):
        return f"{sym}.CN"
    # NEOE = Cboe Canada (NEO) 现行交易所代号, 与 contract_spec 的默认值对应
    if exkey in ("NEO", "NEOEX", "NEOE"):
        return f"{sym}.NE"
    if ccy == "AUD" or exkey == "ASX":
        return f"{sym}.AX"
    if ccy == "SGD" or exkey in ("SGX", "SES"):
        return f"{sym}.SI"
    if ccy == "BRL" or exkey in ("BVMF", "BOVESPA", "B3"):
        return f"{sym}.SA"
    if ccy == "CAD" or exkey in ("TSE", "TSXV", "TSX", "CDGX"):
        return f"{sym}.V" if exkey == "TSXV" else f"{sym}.TO"
    if ccy == "USD" or exkey in (
        "SMART",
        "NYSE",
        "NASDAQ",
        "AMEX",
        "ARCA",
        "BATS",
        "ISLAND",
        "PSX",
        "DRCTEDGE",
    ):
        if exkey == "SHSE":
            return f"{sym}.SS"
        return sym
    return None


def get_history_ibkr(
    parsed: list[ParsedSymbol],
    months: int,
    cfg: dict | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[dict[str, pd.DataFrame], str | None]:
    import pandas as pd

    claimed = [p for p in parsed if _claims_global(p)]
    if not claimed:
        return {}, None
    cfg = cfg or load_config()
    ib = _get_client(cfg)
    if ib is None:
        reason = _unavailable[0] if _unavailable else "不可用"
        return {}, reason
    m = _ib_module()
    results: dict[str, pd.DataFrame] = {}
    first_err: str | None = None
    for p in claimed:
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
            # 显式区间优先 (endDateTime 为 IBKR 时区本地时间; 区间截断在调用方
            # 之外的 orchestration 不做, 必须在此完成), 否则按 months 回看至今
            if start_date:
                end_dt = end_date or date.today().isoformat()
                end_tm = datetime.fromisoformat(end_dt + "T23:59:59")
                # 闭区间天数: end-start 差 1, 补 +1 (2024-01-01→2024-01-31 = 31 D)
                days = (end_tm.date() - date.fromisoformat(start_date)).days + 1
                # IBKR 1 日K单次请求上限 365 D: 超限会被拒 (进而静默回落 yfinance),
                # 故封顶 365 (取最近一年; 更长的区间需分块, 暂不实现)
                duration = f"{min(max(days, 1), 365)} D"
            else:
                end_tm = None
                duration = f"{months} M"
            bars = ib.reqHistoricalData(
                qualified[0],
                endDateTime=end_tm or "",
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
            if bars and start_date:
                # IBKR 的 endDateTime 含当日: 过滤到 [start, end] 闭区间
                bars = [b for b in bars if start_date <= b.date.isoformat() <= end_dt]
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
        except Exception as e:  # noqa: BLE001 - 单代码失败不拖垮整批
            # 记录首个失败原因, 避免全部落空时调用方拿到空结果却无 reason 可感知
            if first_err is None:
                first_err = f"{p.yahoo}: {e}" or type(e).__name__
            continue
    return results, first_err


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
    return [
        pos for pos in ib.positions() if getattr(pos.contract, "secType", "") == "STK"
    ]


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
