"""数据源回退顺序: 配置读取 / 默认链 / 前端编辑器元数据.

设置页的拖动编辑器与 provider 源链共用本模块, 避免「UI 显示的顺序」与
「真实取数顺序」各写一套而逐渐分叉。

配置位置: ``settings.json`` 的 ``source_order``::

    "source_order": {
      "global": {
        "quote":   ["eodhd", "yfinance", "akshare"],
        "history": ["eodhd", "yfinance", "akshare"]
      },
      "cn":      {"quote": ["akshare", "yfinance"], "history": ["akshare", "yfinance"]},
      "crypto":  {"quote": ["binance", "gate", "hyperliquid", "yfinance"], ...},
      "index":   {"history": ["akshare", "yfinance"]}
    }

约定:
- 列表按优先级排列; 手写配置里未列出的源会自动补到末尾 (按目录顺序),
  避免新增数据源后旧配置永远用不到它。可选源仍受 IBKR/长桥/EODHD 开关约束。
- 空/缺失 = 沿用历史默认链, 保证老配置零改动.
- 大小写、``gate.io``/``yahoo`` 等别名在读取时归一.
- 显式顺序优先于 EODHD 的 ``priority`` / ``quotes`` 角色, 但 ``quotes=off``
  与未配置 token 仍会跳过 EODHD。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass

__all__ = [
    "DOMAIN_ORDER",
    "CHANNEL_ORDER",
    "CATALOG",
    "DOMAIN_META",
    "SOURCE_META",
    "CHANNEL_LABELS",
    "normalize",
    "configured",
    "signature",
    "clear_cache",
    "default_order",
    "display_order",
    "label",
    "chain_text",
    "prefer_source",
]

DOMAIN_ORDER = ("global", "cn", "crypto", "index")
CHANNEL_ORDER = ("quote", "history")

#: 每个域、每个通道可排序的逐代码数据源 (批量前置的 IBKR/长桥不在此列)
CATALOG: dict[str, dict[str, tuple[str, ...]]] = {
    "global": {
        "quote": ("eodhd", "yfinance", "akshare"),
        "history": ("eodhd", "yfinance", "akshare"),
    },
    "cn": {
        "quote": ("akshare", "yfinance"),
        "history": ("akshare", "yfinance"),
    },
    "crypto": {
        "quote": ("binance", "gate", "hyperliquid", "yfinance"),
        "history": ("binance", "gate", "hyperliquid", "yfinance"),
    },
    "index": {
        "quote": (),
        "history": ("akshare", "yfinance"),
    },
}

DOMAIN_META = {
    "global": (
        "全球股票",
        "美股 / 港股 / 德英加澳 / 日韩。akshare 仅用于港股，非港股代码会自动跳过。",
    ),
    "cn": ("A股 / 北交所", "沪深北股票与 B 股。长桥批量前置由上方开关控制。"),
    "crypto": (
        "加密货币",
        "BASE-QUOTE 币对（BTC-USD）。Hyperliquid 为永续价，仅 USD 系计价。",
    ),
    "index": (
        "指数（历史K线）",
        "IX.* 指数只有历史数据；水泥网 / 恒生 / Cameco / 商品期货等专属源不参与排序。",
    ),
}

CHANNEL_LABELS = {
    "quote": "实时行情",
    "history": "历史K线",
}


@dataclass(frozen=True)
class SourceMeta:
    label: str
    note: str = ""


SOURCE_META: dict[str, SourceMeta] = {
    "eodhd": SourceMeta("EODHD（BYOK）", "需 token；行情 quotes=off 时禁用"),
    "yfinance": SourceMeta("yfinance / OpenBB", "免 Key；Yahoo Finance"),
    "akshare": SourceMeta("akshare / 东财", "全球域仅港股使用"),
    "binance": SourceMeta("Binance", "现货 24h ticker"),
    "gate": SourceMeta("Gate.io", "现货；覆盖 Binance 未上架币种"),
    "hyperliquid": SourceMeta(
        "Hyperliquid", "永续 markPx；仅 USD 系计价，与现货有基差"
    ),
    "longport": SourceMeta("长桥 LongPort", "批量前置；需登录，见上方开关"),
    "ibkr": SourceMeta("IBKR", "批量前置；需 IB Gateway，见上方开关"),
}

# 手写配置的常见别名 → 内部 id (Quote.source 的 gate.io 也映射到 gate)
_ALIASES = {
    "yahoo": "yfinance",
    "yf": "yfinance",
    "eod": "eodhd",
    "gate.io": "gate",
    "gateio": "gate",
    "hl": "hyperliquid",
    "hyper": "hyperliquid",
}

# settings.json 的缓存: 源链在批量取数时按代码反复查询, 每次读盘/解析不可接受.
# 键含 inode/mtime/size, 设置页原子替换或手改文件后自动失效。
_cache_key: tuple | None = None
_cache_value: dict = {}
_cache_lock = threading.Lock()


def _settings_source_order() -> dict:
    """读取 settings.json 的 source_order (带文件身份缓存)."""
    global _cache_key, _cache_value
    from . import storage

    path = storage.SETTINGS_PATH
    try:
        st = path.stat()
        key: tuple = (
            str(path),
            st.st_dev,
            st.st_ino,
            st.st_mtime_ns,
            st.st_size,
        )
    except OSError:
        key = (str(path), None)
    with _cache_lock:
        if _cache_key == key:
            return _cache_value
    try:
        settings = storage.load_settings(path)
    except Exception:  # noqa: BLE001 - 配置错误不应阻断取数
        settings = {}
    raw = settings.get("source_order")
    value = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    with _cache_lock:
        _cache_key = key
        _cache_value = value
    return value


def clear_cache() -> None:
    """清空设置缓存 (测试 / 设置写入后显式刷新)."""
    global _cache_key, _cache_value
    with _cache_lock:
        _cache_key = None
        _cache_value = {}


def _canonical(source_id: object) -> str | None:
    if not isinstance(source_id, str):
        return None
    s = source_id.strip().lower()
    return _ALIASES.get(s, s)


def normalize(domain: str, channel: str, raw) -> list[str] | None:
    """校验/归一一条源链; 空或全非法返回 None (由调用方回退默认链).

    未列出的合法源补到末尾, 保证「手写了一个旧列表」不会让后来新增的数据源
    永远不可用。列表顺序本身仍完全由用户决定。
    """
    valid = CATALOG.get(domain, {}).get(channel)
    if not valid or not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    for item in raw:
        source_id = _canonical(item)
        if source_id in valid and source_id not in out:
            out.append(source_id)
    if not out:
        return None
    for source_id in valid:
        if source_id not in out:
            out.append(source_id)
    return out


def configured(domain: str, channel: str) -> list[str] | None:
    """用户在 settings.json 中显式配置的源链; 未配置返回 None."""
    raw = _settings_source_order().get(domain)
    if not isinstance(raw, dict):
        return None
    return normalize(domain, channel, raw.get(channel))


def signature() -> str:
    """源顺序签名 (进 K线磁盘缓存键, 手改配置后不会命中旧源的历史数据)."""
    raw = _settings_source_order()
    if not raw:
        return ""
    try:
        blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _move_front(order: list[str], source_id: str | None) -> list[str]:
    if not source_id or source_id not in order:
        return order
    return [source_id] + [s for s in order if s != source_id]


def prefer_source(order: list[str], prefer) -> list[str]:
    """把显式偏好源 (--akshare / --crypto-source) 临时前置.

    bool 是其它域 ``prefer_first`` 的旧语义, 本函数不处理 (由调用方映射)。
    """
    if isinstance(prefer, bool) or not prefer:
        return order
    return _move_front(order, _canonical(prefer))


def default_order(
    domain: str,
    channel: str,
    *,
    prefer_akshare: bool = False,
    crypto_source: str = "auto",
    eodhd_quotes: str = "fallback",
    eodhd_priority: str = "primary",
    akshare_supported: bool = True,
) -> list[str]:
    """未配置 source_order 时, 当前设置对应的默认链 (供 UI 展示/恢复默认)."""
    if domain == "global":
        # 港股 akshare 是唯一需要翻转的; 非港股在 provider 里会跳过该源。
        if channel == "quote":
            base = (
                ["eodhd", "yfinance", "akshare"]
                if eodhd_quotes == "primary"
                else ["yfinance", "akshare", "eodhd"]
            )
        else:
            base = (
                ["eodhd", "yfinance", "akshare"]
                if eodhd_priority == "primary"
                else ["yfinance", "akshare", "eodhd"]
            )
        if prefer_akshare:
            base = _move_front(base, "akshare")
        return base
    if domain == "cn":
        return ["akshare", "yfinance"]
    if domain == "crypto":
        base = ["binance", "gate", "hyperliquid", "yfinance"]
        if crypto_source != "auto":
            base = _move_front(base, _canonical(crypto_source))
        return base
    if domain == "index":
        if not akshare_supported:
            return ["yfinance"]
        return ["akshare", "yfinance"]
    return list(CATALOG.get(domain, {}).get(channel, ()))


def display_order(
    domain: str,
    channel: str,
    *,
    prefer_akshare: bool = False,
    crypto_source: str = "auto",
    eodhd_quotes: str = "fallback",
    eodhd_priority: str = "primary",
    akshare_supported: bool = True,
) -> list[str]:
    """UI 展示用顺序: 已配置则用配置, 否则用默认链."""
    configured_order = configured(domain, channel)
    if configured_order is not None:
        # 与 provider 一致: 旧版快捷偏好仍可临时前置 (设置页保存顺序时会把
        # 该域的快捷偏好复位, 因此正常 UI 路径下这里不会改写用户拖好的顺序)。
        order = list(configured_order)
        if domain == "global" and prefer_akshare and "akshare" in order:
            order = ["akshare"] + [s for s in order if s != "akshare"]
        elif domain == "crypto":
            order = prefer_source(order, crypto_source)
        return order
    return default_order(
        domain,
        channel,
        prefer_akshare=prefer_akshare,
        crypto_source=crypto_source,
        eodhd_quotes=eodhd_quotes,
        eodhd_priority=eodhd_priority,
        akshare_supported=akshare_supported,
    )


def label(source_id: str) -> str:
    meta = SOURCE_META.get(source_id)
    return meta.label if meta else source_id


def chain_text(order: list[str]) -> str:
    return " → ".join(label(s) for s in order)
