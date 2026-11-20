"""K线页会话状态与取数缓存 (与 Streamlit 渲染解耦).

从 kline_page.py 拆出: 快捷/最近代码、单票缓存失效令牌、st.cache_data
包装与市值对比缓存。页面模块保留组件实例与渲染逻辑, 并通过再导出保持
既有 patch/调用路径。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tracker import mcap, prices
from tracker.symbols import parse

# 快捷入口: 持仓/自选可能很长, 单只查询页只展示前 N 个 (数量/成本顺序, 无行情排序)
QUICK_SYMBOLS_LIMIT = 10
KLINE_SYMBOLS_KEY = "kline_quick_symbols"
# 最近查看: 会话级记录最近成功渲染 K线的代码 (最新在前, 去重, 上限 RECENT_LIMIT)
RECENT_KEY = "kline_recent_symbols"
RECENT_LIMIT = 10
# 单代码强制刷新令牌: {symbol: n}. 每次「强制刷新」只递增当前代码的令牌,
# 让该代码的 st.cache_data 键失效 —— 不清空其它代码的缓存 (单票刷新不应
# 连累整个页面的其它查询)。
BUST_KEY = "_kline_cache_bust"


def _source_state(
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
) -> tuple:
    """影响 K 线实际来源的全部配置 (会话数据失效键).

    不能只看四个布尔/枚举参数: 设置页的拖动源顺序、EODHD 角色/复权/
    币种覆盖同样会改变 prices.get_ohlc 的结果。缺少这些签名时, 改完设置
    回到 K线页会继续展示旧源的会话数据, 直到换代码或点强制刷新。
    """
    from tracker import eodhd, source_order

    return (
        prefer_akshare,
        use_ibkr,
        use_longport,
        crypto_source,
        source_order.signature(),
        eodhd.cache_signature(),
    )


def _source_state(
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
) -> tuple:
    """影响 K 线实际来源的全部配置 (会话数据失效键).

    不能只看四个布尔/枚举参数: 设置页的拖动源顺序、EODHD 角色/复权/
    币种覆盖同样会改变 prices.get_ohlc 的结果。缺少这些签名时, 改完设置
    回到 K线页会继续展示旧源的会话数据, 直到换代码或点强制刷新。
    """
    from tracker import eodhd, source_order

    return (
        prefer_akshare,
        use_ibkr,
        use_longport,
        crypto_source,
        source_order.signature(),
        eodhd.cache_signature(),
    )

def quick_symbols() -> list[str]:
    """持仓+自选代码 (页面启动时注入, K线页自身不发起任何网络请求)."""
    return list(st.session_state.get(KLINE_SYMBOLS_KEY, []))

def ensure_quick_symbols_from_storage() -> None:
    """直接打开 /kline (未经过组合页) 时从磁盘补齐「常用」代码.

    组合页每次渲染都会 set_quick_symbols, 但新会话直接访问 /kline、或刷新
    浏览器后 session_state 丢失时, 该键不存在, 「常用」pills 会是空列表。
    只在键缺失时读盘, 不覆盖组合页已经算好的快照 (含编辑器未保存内容)。
    """
    if KLINE_SYMBOLS_KEY in st.session_state:
        return
    try:
        from tracker import storage
        from tracker.services.watchlist import merge_entries

        portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
        watch = storage.load_watchlist(storage.WATCHLIST_PATH)
        symbols = [
            str(h.get("symbol", "")).strip()
            for h in portfolio.get("holdings", [])
        ]
        symbols += [str(e.get("symbol", "")).strip() for e in merge_entries(watch)]
    except Exception:  # noqa: BLE001 - 读盘失败时保持空列表, 不影响查询
        return
    set_quick_symbols([s for s in symbols if s])

def recent_symbols() -> list[str]:
    """本会话最近成功渲染 K线的代码 (最新在前, 上限 RECENT_LIMIT)."""
    return list(st.session_state.get(RECENT_KEY, []))

def record_recent(symbol: str) -> bool:
    """查询成功渲染后调用: 记录进最近查看 (去重, 最新在前, 截断保留).

    返回列表是否发生变化 (供调用方决定是否 st.rerun 让 pills 立即显示)。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return False
    old = recent_symbols()
    new = ([s] + [x for x in old if x != s])[:RECENT_LIMIT]
    if new == old:
        return False
    st.session_state[RECENT_KEY] = new
    return True

def set_quick_symbols(symbols: list[str]) -> None:
    st.session_state[KLINE_SYMBOLS_KEY] = list(dict.fromkeys(symbols))

def _bust_token(symbol: str) -> int:
    """该代码当前的缓存失效令牌 (默认 0)."""
    tokens = st.session_state.setdefault(BUST_KEY, {})
    return int(tokens.get(str(symbol).strip().upper(), 0))

def bump_kline_cache(symbols) -> None:
    """只递增指定代码的缓存令牌 (单票强制刷新, 不影响其它代码缓存)."""
    tokens = st.session_state.setdefault(BUST_KEY, {})
    for s in dict.fromkeys(str(x).strip().upper() for x in symbols if x):
        tokens[s] = int(tokens.get(s, 0)) + 1

@st.cache_data(ttl=1800, show_spinner=False)
def cached_kline(
    symbol: str,
    months: int,
    prefer_akshare: bool,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
    refresh: bool = False,
    cache_bust: int = 0,
):
    """K线日线 (磁盘缓存 + 内存缓存双层, TTL 内切换参数不重复请求网络).

    refresh=True 跳过磁盘 OHLC 缓存 (其键只有 symbol+months, 不含数据源偏好):
    数据源偏好切换后必须重取, 否则被旧源的磁盘缓存短路, 继续展示上一个源的K线
    (与滑动模式 cached_kline_all 同口径)。
    cache_bust: 单代码强制刷新令牌。令牌变化的代码使用全新 st.cache_data 键,
    只失效该代码, 不连累其它代码的缓存。
    """
    return prices.get_ohlc(
        symbol,
        months=months,
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
        crypto_source=crypto_source,
        refresh=refresh,
    )

@st.cache_data(ttl=3600, show_spinner=False)
def cached_kline_all(
    symbol: str,
    prefer_akshare: bool,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
    refresh: bool = False,
    cache_bust: int = 0,
):
    """上市以来全量日K (滑动模式唯一取数路径, 缓存键独立于范围模式月份参数).

    refresh=True 跳过磁盘 OHLC 缓存 (其键只有 symbol+months, 不含数据源
    偏好): 设置页切换 prefer_akshare/IBKR/长桥/crypto_source 后必须重取,
    否则「重新取数」会被旧源的磁盘缓存短路, 继续展示上一个源的K线。
    cache_bust: 单代码强制刷新令牌 (只失效当前代码的页面缓存)。
    """
    return prices.get_ohlc(
        symbol,
        months=1200,
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
        crypto_source=crypto_source,
        refresh=refresh,
    )

def is_valid_symbol(s: str) -> bool:
    if not s or not s.strip():
        return False
    try:
        parse(s)
    except ValueError:
        return False
    return True

@st.cache_data(ttl=3600, show_spinner=False)
def cached_mcap_compare(
    symbols: tuple[str, ...],
    months: int,
    prefer_akshare: bool,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
    target_currency: str | None = None,
    refresh: bool = False,
):
    """历史市值对比序列 (磁盘缓存 + 内存缓存双层): 市值口径不可变属性低频.

    内部经 cached_kline 取同源价格K线, 市值序列按「东财历史总市值 / 历史股本
    ×收盘 / 当前市值近似」三级兜底, 最后统一折算 target_currency 币种。
    返回 (市值 frames dict, warnings 列表, 元信息 dict)。

    refresh=True 穿透 K线/市值序列/基本面负缓存 (页面「强制刷新」)。
    """
    frames: dict[str, pd.DataFrame] = {}
    for s in symbols:
        # 与下方股价对比路径 (render_compare_controls) 同口径: 单个代码取数
        # 失败只跳过该代码, 不能让异常穿出 st.cache_data 把整页打死。
        try:
            d = cached_kline(
                s,
                months,
                prefer_akshare,
                use_ibkr,
                use_longport,
                crypto_source,
                refresh=refresh,
            )
        except Exception:  # noqa: BLE001 - 单代码失败不拖垮整页
            continue
        if isinstance(d, pd.DataFrame) and not d.empty:
            frames[s] = d
    if refresh:
        cap_frames, warnings, meta = mcap.compare_market_caps(
            frames, target_currency, refresh=True
        )
    else:
        cap_frames, warnings, meta = mcap.compare_market_caps(
            frames, target_currency
        )
    return cap_frames, warnings, meta

def clear_kline_caches() -> None:
    """清 K线/市值对比的 st.cache_data 层 (强制刷新用).

    磁盘层由 get_ohlc(refresh=True) 穿透; 但 st.cache_data 会把「空结果/
    失败告警」缓存 30-60 分钟, 只靠磁盘 TTL 或负缓存过期无法手动重试。
    """
    cached_kline.clear()
    cached_kline_all.clear()
    cached_mcap_compare.clear()

