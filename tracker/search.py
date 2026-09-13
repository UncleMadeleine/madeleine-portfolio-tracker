"""代码搜索 (provider 目录聚合): 按域取目录, 模糊匹配, 聚合结果.

每个 provider 负责自己域的代码目录 (catalog), search 只做:
  1. 聚合三个域目录 (任一域失败不影响其它域)
  2. 本地 JSON 缓存 (TTL 1 天)
  3. 纯函数模糊匹配 (评分: 前缀 > 包含 > 子串, 代码优先于名称)
  4. 无缓存时降级为代码格式校验式匹配 (经 symbols.parse)

域隔离: GlobalStocks=美股/港股 (+常见美股代码兜底), CN=A股/北交所 (akshare),
Crypto=Binance 全部现货交易对 (TRADING 状态)。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .util import with_timeout

# 本地缓存文件 (data/ 已在 .gitignore, 不入库)
CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "symbol_list.json"
# 缓存有效期: 1 天 (86400 秒); 收盘后列表基本不变
CACHE_TTL = 86400
# akshare 接口超时 (秒), 避免卡死 UI
_FETCH_TIMEOUT = 20.0
_HK_SINA_TIMEOUT = 90.0  # sina 港股列表分页抓取 (~2 分钟), 超时上限略低


def _ak_quiet():
    """akshare + 抑制其 tqdm 进度条 (污染非 TTY 输出与 JSON 管道)."""
    import io
    import sys

    try:
        from tqdm import tqdm

        class _Quiet(tqdm):
            def display(self, *a, **kw):  # 不刷新任何输出
                pass

        saved_tqdm = sys.modules.get("tqdm.std") or sys.modules.get("tqdm")
        if saved_tqdm is not None and hasattr(saved_tqdm, "tqdm"):
            saved_tqdm.tqdm = _Quiet
    except ImportError:
        pass
    import akshare as ak
    return ak


def _redirect_stdout_quiet(fn, *args, **kwargs):
    """彻底静默: fd 1/2 均重定向 devnull 执行 fn (akshare/tqdm 进度条走 stderr)."""
    import contextlib
    import io
    import os
    import sys

    devnull = os.open(os.devnull, os.O_WRONLY)
    saved1, saved2 = os.dup(1), os.dup(2)
    try:
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:
                pass
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)
    finally:
        os.dup2(saved1, 1)
        os.dup2(saved2, 2)
        os.close(saved1)
        os.close(saved2)



# Binance 目录仅保留这些计价货币 (与 symbols._CRYPTO_QUOTES 的主流子集一致)
_CRYPTO_QUOTE_ASSETS = ("USDT", "USDC", "USD", "FDUSD", "EUR", "BTC", "ETH", "BNB")


@dataclass(frozen=True)
class SymbolEntry:
    """一条代码记录 (规范代码 + 名称 + 市场标签 + 权威域标记)."""

    code: str  # 规范代码, 如 600519.SS / 0700.HK / AAPL / BTC-USD
    name: str  # 中文名/英文名/交易对, 如 贵州茅台 / APPLE INC / BTCUSDT
    market: str  # 市场标签, 如 A股 / 港股 / 美股 / 加密货币
    type: str = "global"  # 权威域标记: global / cn / crypto (系统推导)


def _ak():
    import akshare as ak

    return ak


# ---------- GlobalStocks provider 目录 (美股/港股) ----------


def _cn_suffix(code: str) -> str:
    """A股纯数字代码 → 交易所后缀 (SS/SZ/BJ)."""
    if code.startswith(("4", "8")) or code.startswith("920"):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SS"
    return "SZ"


def _to_yahoo_cn(code: str) -> str:
    return f"{code}.{_cn_suffix(code)}"


def _to_yahoo_hk(code: str) -> str:
    """港股代码 → Yahoo 规范 (4 位补零 + .HK)."""
    return f"{int(code):04d}.HK"


def fetch_a_symbols() -> list[SymbolEntry]:
    """A股全量代码+名称 (akshare stock_info_a_code_name, 轻量接口)."""
    try:
        df = _redirect_stdout_quiet(
            with_timeout, _ak_quiet().stock_info_a_code_name, _FETCH_TIMEOUT
        )
    except Exception:
        return []
    out: list[SymbolEntry] = []
    for _, row in df.iterrows():
        code = str(row["code"]).strip()
        name = str(row["name"]).strip()
        if code and name:
            out.append(SymbolEntry(_to_yahoo_cn(code), name, "A股", "cn"))
    return out


def fetch_hk_symbols() -> list[SymbolEntry]:
    """港股全量代码+名称 (akshare stock_hk_spot, 新浪源, 含中文名).

    东财 stock_hk_spot_em 对部分数据中心 IP 不可达 (连接被重置);
    新浪源 stock_hk_spot 分页抓取较慢 (~1-2 分钟) 但稳定, 供后台目录刷新。
    """
    try:
        df = _redirect_stdout_quiet(
            with_timeout, _ak_quiet().stock_hk_spot, _HK_SINA_TIMEOUT
        )
    except Exception:
        return []
    out: list[SymbolEntry] = []
    # 新浪源列: 代码 (如 00700) / 中文名称
    code_col = "代码" if "代码" in df.columns else df.columns[0]
    name_col = "名称" if "名称" in df.columns else df.columns[1]
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip()
        if code and name:
            try:
                out.append(SymbolEntry(_to_yahoo_hk(code), name, "港股", "global"))
            except (ValueError, TypeError):
                continue
    return out


def fetch_us_symbols() -> list[SymbolEntry]:
    """美股代码+名称.

    akshare 美股全量接口 (get_us_stock_name) 需爬取数百页, 过慢且易超时,
    默认不拉取 —— 美股仅靠代码模糊匹配 (search_symbols 内置常见代码兜底)。
    """
    return []


def fetch_global_symbols() -> list[SymbolEntry]:
    """全球股票域目录: 港股 (akshare) + 美股 (暂缓, 靠代码兜底)."""
    return fetch_hk_symbols() + fetch_us_symbols()


# ---------- CN provider 目录 (A股/北交所) ----------


def fetch_cn_symbols() -> list[SymbolEntry]:
    """A股/北交所域目录: akshare 全量代码+名称."""
    return fetch_a_symbols()


# ---------- Crypto provider 目录 (Binance 现货) ----------


def fetch_crypto_symbols() -> list[SymbolEntry]:
    """加密货币域目录: Binance exchangeInfo 全部 TRADING 现货交易对.

    Binance 交易对形态 BTCUSDT → 规范代码 BTC-USD (无 USDT 计价的用原币种,
    如 ETHBTC → ETH-BTC; 均为 symbols.parse 可识别的 crypto 代码)。
    """
    from .providers.crypto import _get

    try:
        info = with_timeout(_get, _FETCH_TIMEOUT, "/api/v3/exchangeInfo")
    except Exception:
        return []
    out: list[SymbolEntry] = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or not s.get("isSpotTradingAllowed", False):
            continue
        quote = s.get("quoteAsset", "")
        if quote not in _CRYPTO_QUOTE_ASSETS:
            continue
        pair = f"{s['baseAsset']}-{quote}"
        out.append(SymbolEntry(pair, s["symbol"], "加密货币", "crypto"))
    return out


# ---------- 本地缓存 ----------


def _fetch_all() -> list[SymbolEntry]:
    """聚合各域目录 (任一域失败不影响其它域)."""
    entries: list[SymbolEntry] = []
    entries += fetch_cn_symbols()
    entries += fetch_global_symbols()
    entries += fetch_crypto_symbols()
    return entries


def load_symbol_list(force: bool = False) -> list[SymbolEntry]:
    """加载代码目录: 优先读本地缓存 (TTL 内), 过期或缺失则请求网络刷新.

    网络全部失败时返回空列表 (调用方降级为纯代码模糊匹配)。
    """
    if not force and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text("utf-8"))
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return [SymbolEntry(**e) for e in data.get("entries", [])]
        except Exception:
            pass
    entries = _fetch_all()
    if entries:
        _save_cache(entries)
    return entries


def _save_cache(entries: list[SymbolEntry]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(
                {"fetched_at": time.time(), "entries": [asdict(e) for e in entries]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


# ---------- 模糊匹配 (纯函数, 便于测试) ----------


def _score(query: str, code: str, name: str) -> int:
    """匹配评分 (越高越靠前): 前缀 > 包含 > 子串, 代码优先于名称."""
    q = query.lower()
    c = code.lower()
    n = name.lower()
    score = 0
    if c == q:
        score += 1000
    elif c.startswith(q):
        score += 500
    elif q in c:
        score += 200
    if n == q:
        score += 800
    elif n.startswith(q):
        score += 400
    elif q in n:
        score += 150
    return score


def _match(entries: list[SymbolEntry], query: str, limit: int = 10) -> list[SymbolEntry]:
    """对已加载目录做模糊匹配, 按评分降序返回前 limit 条."""
    q = query.strip().lower()
    if not q:
        return []
    scored: list[tuple[int, SymbolEntry]] = []
    for e in entries:
        s = _score(q, e.code, e.name)
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda t: (-t[0], t[1].code))
    return [e for _, e in scored[:limit]]


def search_symbols(
    query: str, limit: int = 10, entries: list[SymbolEntry] | None = None
) -> list[dict]:
    """按代码或名称模糊搜索代码, 返回 [{code, name, market}] 列表.

    entries 为 None 时自动加载本地缓存; 传入则直接匹配 (便于测试与复用)。
    网络不可用 / 缓存为空时降级为纯代码模糊匹配 (见 _fallback_match)。
    """
    if entries is None:
        entries = load_symbol_list()
    results = _match(entries, query, limit)
    if results:
        return [asdict(e) for e in results]
    # 降级: 缓存为空时, 至少对查询本身做代码格式校验式匹配
    return _fallback_match(query, limit)


def _fallback_match(query: str, limit: int) -> list[dict]:
    """无缓存时的降级: 把查询当作代码, 经 parse 校验合法后原样返回."""
    from .symbols import parse

    q = query.strip().upper()
    if not q:
        return []
    try:
        p = parse(q)
    except Exception:
        return []
    return [
        {"code": p.yahoo, "name": q, "market": p.market_label, "type": p.type}
    ][:limit]


def refresh_cache() -> int:
    """强制刷新本地缓存, 返回写入条数 (供设置页/CLI 调用)."""
    entries = _fetch_all()
    if entries:
        _save_cache(entries)
    return len(entries)
