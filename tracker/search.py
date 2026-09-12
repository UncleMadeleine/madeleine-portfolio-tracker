"""股票搜索: 按代码或名称模糊匹配 (akshare 取数 + 本地 JSON 缓存).

数据源优先级:
1. 本地缓存 `data/symbol_list.json` (TTL 1 天, 避免每次输入都请求网络)
2. akshare 股票列表接口 (A股/港股含中文名称; 美股接口过慢, 仅支持代码模糊匹配)

搜索逻辑纯函数化 (`_match`), 便于单元测试, 不依赖网络。
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


@dataclass(frozen=True)
class SymbolEntry:
    """一条股票记录 (Yahoo 规范代码 + 中文名 + 市场标签)."""

    code: str  # Yahoo 规范代码, 如 600519.SS / 0700.HK / AAPL
    name: str  # 中文名/英文名, 如 贵州茅台 / APPLE INC
    market: str  # 市场标签, 如 A股 / 港股 / 美股


def _ak():
    import akshare as ak

    return ak


# ---------- A股代码 → Yahoo 规范后缀 ----------


def _cn_suffix(code: str) -> str:
    """A股纯数字代码 → 交易所后缀 (SS/SZ/BJ).

    沪市(.SS): 60/68/90 开头 (主板/科创板/B股)
    北交所(.BJ): 4/8 开头
    其余按深市(.SZ)处理 (00主板/30创业板/20B股)
    """
    if code.startswith(("60", "68", "90", "11", "50")):
        return "SS"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def _to_yahoo_cn(code: str) -> str:
    return f"{code}.{_cn_suffix(code)}"


def _to_yahoo_hk(code: str) -> str:
    """港股代码 → Yahoo 规范 (4 位补零 + .HK)."""
    return f"{int(code):04d}.HK"


# ---------- akshare 取数 (各市场独立, 失败不影响其它) ----------


def fetch_a_symbols() -> list[SymbolEntry]:
    """A股全量代码+名称 (akshare stock_info_a_code_name, 轻量接口)."""
    try:
        df = with_timeout(_ak().stock_info_a_code_name, _FETCH_TIMEOUT)
    except Exception:
        return []
    out: list[SymbolEntry] = []
    for _, row in df.iterrows():
        code = str(row["code"]).strip()
        name = str(row["name"]).strip()
        if code and name:
            out.append(SymbolEntry(_to_yahoo_cn(code), name, "A股"))
    return out


def fetch_hk_symbols() -> list[SymbolEntry]:
    """港股全量代码+名称 (akshare stock_hk_spot_em, 含中文名)."""
    try:
        df = with_timeout(_ak().stock_hk_spot_em, _FETCH_TIMEOUT)
    except Exception:
        return []
    out: list[SymbolEntry] = []
    # 列名兼容: 东财接口为「代码」「名称」
    code_col = "代码" if "代码" in df.columns else df.columns[0]
    name_col = "名称" if "名称" in df.columns else df.columns[1]
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip()
        if code and name:
            try:
                out.append(SymbolEntry(_to_yahoo_hk(code), name, "港股"))
            except (ValueError, TypeError):
                continue
    return out


def fetch_us_symbols() -> list[SymbolEntry]:
    """美股代码+名称.

    akshare 美股全量接口 (get_us_stock_name) 需爬取数百页, 过慢且易超时,
    默认不拉取 —— 美股仅靠代码模糊匹配 (search_symbols 内置常见代码兜底)。
    """
    return []


# ---------- 本地缓存 ----------


def _fetch_all() -> list[SymbolEntry]:
    """聚合各市场列表 (任一失败不影响其它市场)."""
    entries: list[SymbolEntry] = []
    entries += fetch_a_symbols()
    entries += fetch_hk_symbols()
    entries += fetch_us_symbols()
    return entries


def load_symbol_list(force: bool = False) -> list[SymbolEntry]:
    """加载股票列表: 优先读本地缓存 (TTL 内), 过期或缺失则请求网络刷新.

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
        payload = {"fetched_at": time.time(), "entries": [asdict(e) for e in entries]}
        CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
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
    """对已加载列表做模糊匹配, 按评分降序返回前 limit 条."""
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
    """按代码或名称模糊搜索股票, 返回 [{code, name, market}] 列表.

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
    return [{"code": p.yahoo, "name": q, "market": p.market_label}][:limit]


def refresh_cache() -> int:
    """强制刷新本地缓存, 返回写入条数 (供设置页/CLI 调用)."""
    entries = _fetch_all()
    if entries:
        _save_cache(entries)
    return len(entries)
