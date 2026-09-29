"""长桥 (LongPort) OpenAPI 接入: 可选依赖 longport SDK, 失败自动回退默认数据源.

与 tracker.ibkr 同一套可选增强模式:
  - 行情/历史: orchestration 前置 (settings.json "use_longport": true 启用)
  - 持仓导入: import longport (OAuth 登录后读取账户持仓)

认证两种方式 (配置文件 longport.json, 模板 longport.example.json):
  - oauth: OAuthBuilder 浏览器授权, token 由 SDK 持久化在
    ~/.longport/openapi/tokens/<client_id> 并自动刷新 (推荐, UI 登录引导走此路径)
  - apikey: 开发者中心 User Center 的 app_key/app_secret/access_token (HMAC 由 SDK 处理)

代码映射 (内部 Yahoo 规范 ↔ 长桥 ticker.region):
  AAPL ↔ AAPL.US / 0700.HK ↔ 700.HK (长桥不补零) / 600519.SS ↔ 600519.SH /
  000001.SZ ↔ 000001.SZ / 830799.BJ ↔ 无 (长桥暂不支持北交所)
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .providers.base import Quote
from .symbols import parse, type_for_symbol
from .symbol_migrations import migrate_symbol

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "longport.json"
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "longport.example.json"

# 兜底: 无任何配置文件时的非敏感默认 (auth=oauth 需用户在 longport.json 填 client_id)
_FALLBACK: dict = {
    "auth": "oauth",
}

# 长桥单次 quote/static 请求上限 500; 保守分批避开边界
_BATCH = 200

_UNAVAILABLE_TTL = 60.0
_quote_ctx = None
_unavailable: tuple[str, float] | None = None
_lock = threading.Lock()


def _resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        p = Path(path).expanduser()
        if p.exists():
            return p
    env = os.environ.get("LONGPORT_CONFIG")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    if DEFAULT_CONFIG.exists():
        return DEFAULT_CONFIG
    return EXAMPLE_CONFIG


def _load_file(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_config(path: str | Path | None = None) -> dict:
    """读取长桥配置: 真实文件(或 env/显式 path) 覆盖模板 longport.example.json 覆盖兜底.

    字段: auth (oauth|apikey), client_id (OAuth 客户端), callback_port (OAuth 本地回调端口),
    app_key/app_secret/access_token (apikey), language (en|zh-CN|zh-HK)。
    _comment 开头字段被忽略。真实配置含敏感信息, 已 gitignore。
    """
    cfg = dict(_FALLBACK)
    example = _load_file(EXAMPLE_CONFIG)
    cfg.update({k: v for k, v in example.items() if not k.startswith("_comment")})
    real = _load_file(_resolve_config_path(path))
    cfg.update({k: v for k, v in real.items() if not k.startswith("_comment")})
    return cfg


def reset() -> None:
    """断开缓存的连接上下文 (配置变更/登录成功后调用, 下次取数重建)."""
    global _quote_ctx, _unavailable
    _quote_ctx = None
    _unavailable = None


# ---------------------------------------------------------------------------
# SDK 可用性 / Config 构造
# ---------------------------------------------------------------------------


def _sdk():
    try:
        import longport.openapi as api

        return api
    except ImportError as e:
        raise RuntimeError("未安装 longport (pip install longport)") from e


def _language(api, cfg: dict):
    lang = str(cfg.get("language") or "").strip()
    mapping = {
        "zh-CN": api.Language.ZH_CN,
        "zh-HK": api.Language.ZH_HK,
        "en": api.Language.EN,
    }
    return mapping.get(lang)


def build_config(cfg: dict | None = None):
    """longport.json → SDK Config; 认证方式与凭据错误在此显式报错."""
    api = _sdk()
    cfg = cfg or load_config()
    kwargs = {}
    lang = _language(api, cfg)
    if lang is not None:
        kwargs["language"] = lang
    # 端点覆盖 (可选): SDK 默认端点随账号区域解析, 部分网络环境 (如 .cn 域名
    # 无法解析) 需显式指定国际端点。http_url/quote_ws_url/trade_ws_url 任一可配。
    for key, kw in (
        ("http_url", "http_url"),
        ("quote_ws_url", "quote_ws_url"),
        ("trade_ws_url", "trade_ws_url"),
    ):
        val = str(cfg.get(key) or "").strip()
        if val:
            kwargs[kw] = val
    auth = str(cfg.get("auth") or "oauth").strip().lower()
    if auth == "apikey":
        app_key = str(cfg.get("app_key") or "").strip()
        app_secret = str(cfg.get("app_secret") or "").strip()
        access_token = str(cfg.get("access_token") or "").strip()
        if not (app_key and app_secret and access_token):
            raise RuntimeError(
                "longport.json 缺少 app_key/app_secret/access_token"
                " (auth=apikey 需开发者中心 User Center 的应用凭据)"
            )
        return api.Config.from_apikey(app_key, app_secret, access_token, **kwargs)
    if auth != "oauth":
        raise RuntimeError(f"未知认证方式: {auth!r} (可选: oauth/apikey)")
    client_id = str(cfg.get("client_id") or "").strip()
    if not client_id:
        raise RuntimeError(
            "longport.json 缺少 client_id — 请在开发者中心完成 OAuth 客户端注册"
            "后填入 (文档: https://open.longportapp.com/docs/how-to-access-api)"
        )
    if "填入" in client_id or "YOUR_" in client_id:
        # 模板占位值: 直接报配置错误, 绝不发起授权流 (OAuth 会阻塞等浏览器回调)
        raise RuntimeError(
            "longport.json 的 client_id 仍是模板占位值, 请填入真实 client_id"
        )
    oauth = oauth_login(cfg)
    return api.Config.from_oauth(oauth, **kwargs)


def oauth_login(cfg: dict | None = None, on_open_url=None):
    """OAuth 登录: token 已缓存则直接复用 (不弹浏览器), 否则启动授权流并阻塞.

    SDK 在 ~/.longport/openapi/tokens/<client_id> 缓存 token 并自动刷新。
    on_open_url: 可选回调 (url), 替代默认打印行为 (CLI --json 用它截获 URL)。
    """
    api = _sdk()
    cfg = cfg or load_config()
    client_id = str(cfg.get("client_id") or "").strip()
    if not client_id or "填入" in client_id or "YOUR_" in client_id:
        raise RuntimeError("longport.json 缺少有效 client_id, 无法发起 OAuth 授权")
    port = cfg.get("callback_port")
    builder = (
        api.OAuthBuilder(client_id, int(port)) if port else api.OAuthBuilder(client_id)
    )
    return builder.build(on_open_url or _print_auth_url)


def _print_auth_url(url: str) -> None:
    print(f"请在浏览器中打开以下链接完成长桥授权:\n  {url}")


# ---------------------------------------------------------------------------
# 代码映射: 内部 Yahoo 规范 ↔ 长桥 ticker.region
# ---------------------------------------------------------------------------


def yahoo_to_longport(yahoo: str) -> str | None:
    """内部规范代码 → 长桥 ticker.region; 长桥不支持的市场返回 None.

    AAPL → AAPL.US; 0700.HK → 700.HK (长桥不补零); 600519.SS → 600519.SH;
    000001.SZ 原样; D05.SI → D05.SG; 其它后缀 (.DE/.L/.TO/.AX/.BJ...) 不支持。
    """
    s = str(yahoo).strip().upper()
    head, dot, suffix = s.rpartition(".")
    if not dot:
        return f"{s}.US"  # 裸代码 = 美股 (含 BRK-B 类别股)
    if suffix == "HK":
        return f"{head.lstrip('0') or '0'}.HK"
    if suffix == "SS":
        return f"{head}.SH"
    if suffix == "SZ":
        return f"{head}.SZ"
    if suffix == "SI":
        return f"{head}.SG"
    return None


def longport_to_yahoo(symbol: str) -> str | None:
    """长桥 ticker.region → 内部规范代码; 未知形态返回 None.

    700.HK → 0700.HK (4位补零); 600519.SH → 600519.SS; AAPL.US → AAPL;
    G13.SG → G13.SI; 美股类别股/优先股: TAP.A.US → TAP-A, WFC.PR.L.US 形态
    (Yahoo 惯例: 类别股连字符+单字母, 优先股连字符+P+系列字母)。
    """
    raw = str(symbol).strip().upper()
    if "." not in raw:
        return None
    head, _, region = raw.rpartition(".")
    if region == "HK":
        return f"{head.lstrip('0') or '0'}".zfill(4) + ".HK"
    if region == "SH":
        return f"{head}.SS"
    if region == "SZ":
        return f"{head}.SZ"
    if region == "US":
        # 类别股/优先股: 长桥点分限定 (TAP.A / WFC.PR.L) → Yahoo 连字符惯例
        # (类别股 X-A; 优先股 X-P + 系列字母, 如 WFC-PL)
        parts = head.split(".")
        if len(parts) >= 2:
            base = parts[0]
            qual = parts[1:]
            # PR (preferred) 系列: X.PR.L → X-PL; 类别: X.A → X-A
            if qual[0] == "PR":
                return base + "-P" + "".join(qual[1:])
            return base + "-" + "".join(qual)
        return head
    if region == "SG":
        return f"{head}.SI"  # SGX: Yahoo 规范后缀 .SI (yfinance 无 .SG 数据)
    return None

def _is_unquoted_placeholder(symbol: str) -> bool:
    """长桥未挂牌/未定价占位代码 (打新股 N 前缀等): 拿不到行情, 导入即死代码.

    长桥对未挂牌新股用 N 前缀 ticker (如 N22117.HK); 挂牌后恢复原代码。
    纯数字打头 N + 后缀形态仅占位符使用, 正常上市证券没有这种代码。
    """
    head, _, region = str(symbol).strip().upper().rpartition(".")
    return bool(head) and region in ("HK", "US") and head.startswith("N") and head[1:].isdigit()




# ---------------------------------------------------------------------------
# 连接管理 (QuoteContext 长连接缓存; 失败 TTL 防抖)
# ---------------------------------------------------------------------------


def _drop_quote_ctx() -> None:
    """丢弃缓存的 QuoteContext (疑似死连接): 下次取数重建新连接。"""
    global _quote_ctx
    with _lock:
        _quote_ctx = None


def _ctx_call(fn, *args, cfg: dict | None = None, **kwargs):
    """带死连接重连的 ctx 调用: 首次异常丢弃缓存连接重建一次再试。

    长连接被服务端/网络断开后, SDK 的 QuoteContext 不暴露 isConnected(),
    死连接上再调用只会持续报错 —— 必须在调用层重建。
    """
    ctx = _get_quote_ctx(cfg)
    if ctx is None:
        raise RuntimeError(unavailable_reason() or "长桥连接不可用")
    try:
        return fn(ctx, *args, **kwargs)
    except Exception:
        _drop_quote_ctx()
        ctx = _get_quote_ctx(cfg)
        if ctx is None:
            raise
        return fn(ctx, *args, **kwargs)


def _get_quote_ctx(cfg: dict | None = None):
    global _quote_ctx, _unavailable
    with _lock:
        if _quote_ctx is not None:
            return _quote_ctx
        if _unavailable is not None:
            if time.time() - _unavailable[1] < _UNAVAILABLE_TTL:
                return None
            _unavailable = None
        try:
            api = _sdk()
            ctx = api.QuoteContext(build_config(cfg))
            _quote_ctx = ctx
            return ctx
        except Exception as e:  # noqa: BLE001 - 可选增强: 失败降级并 TTL 防抖
            _unavailable = (f"{type(e).__name__}: {e}"[:200], time.time())
            return None


def unavailable_reason() -> str | None:
    """最近一次连接失败原因 (TTL 内); 供调用方输出提示."""
    if _unavailable is not None and time.time() - _unavailable[1] < _UNAVAILABLE_TTL:
        return _unavailable[0]
    return None


# ---------------------------------------------------------------------------
# 行情 / 历史
# ---------------------------------------------------------------------------


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _currency_for(yahoo: str) -> str:
    try:
        return parse(yahoo).currency
    except ValueError:
        return "USD"


def get_quotes_longport(
    parsed: list, cfg: dict | None = None
) -> tuple[dict[str, Quote], str | None]:
    """批量实时行情 (QuoteContext.quote): 返回 {yahoo: Quote}, 不可用返回 ({}, reason).

    只认领长桥支持的市场 (美股/港股/沪深); 其它市场交回默认数据源。
    """
    mappable = [(p, yahoo_to_longport(p.yahoo)) for p in parsed]
    mappable = [(p, lp) for p, lp in mappable if lp]
    if not mappable:
        return {}, None
    quotes: dict[str, Quote] = {}
    reason = None
    for i in range(0, len(mappable), _BATCH):
        chunk = mappable[i : i + _BATCH]
        try:
            resp = _ctx_call(
                lambda c, syms: c.quote(syms),
                [lp for _, lp in chunk],
                cfg=cfg,
            )
        except Exception as e:  # noqa: BLE001 - 整批失败降级默认源
            reason = f"{type(e).__name__}: {e}"[:200]
            break
        by_lp = {str(getattr(sq, "symbol", "") or "").strip(): sq for sq in resp}
        for p, lp in chunk:
            sq = by_lp.get(lp)
            if sq is None:
                continue
            price = _f(getattr(sq, "last_done", None))
            prev = _f(getattr(sq, "prev_close", None))
            if price is None:
                continue
            chg = (price - prev) / prev * 100.0 if prev else None
            quotes[p.yahoo] = Quote(
                symbol=p.yahoo,
                name=None,
                price=price,
                prev_close=prev,
                change_pct=chg,
                currency=_currency_for(p.yahoo),
            )
    return quotes, reason


def static_names_longport(
    symbols_lp: list[str], cfg: dict | None = None
) -> dict[str, str]:
    """长桥代码 → 名称 (static_info); 失败返回空 dict (名称为可选增强)."""
    if not symbols_lp:
        return {}
    names: dict[str, str] = {}
    for i in range(0, len(symbols_lp), _BATCH):
        chunk = symbols_lp[i : i + _BATCH]
        try:
            for info in _ctx_call(lambda c, syms: c.static_info(syms), chunk, cfg=cfg):
                lp = str(getattr(info, "symbol", "") or "").strip()
                name = (
                    str(getattr(info, "name_cn", "") or "").strip()
                    or str(getattr(info, "name_en", "") or "").strip()
                    or str(getattr(info, "name_hk", "") or "").strip()
                )
                if lp and name:
                    names[lp] = name
        except Exception:  # noqa: BLE001 - 名称缺失不阻塞行情
            break
    return names


def get_history_longport(
    p, start_date: str, end_date: str | None = None, cfg: dict | None = None
) -> pd.DataFrame | None:
    """历史日线 (history_candlesticks_by_date); 不可用/不支持返回 None (调用方回退).

    前复权 (ForwardAdjust) 与 yfinance 口径一致; 输出 date/open/high/low/close/volume 升序。
    """
    lp = yahoo_to_longport(p.yahoo)
    if lp is None:
        return None
    api = _sdk()
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date)
    try:
        candles = _ctx_call(
            lambda c: c.history_candlesticks_by_date(
                lp, api.Period.Day, api.AdjustType.ForwardAdjust, start, end
            ),
            cfg=cfg,
        )
    except Exception:  # noqa: BLE001 - 历史数据不可用即回退默认源
        return None
    if not candles:
        return None
    rows = []
    for c in candles:
        ts = getattr(c, "timestamp", None)
        d = ts.date() if isinstance(ts, datetime) else pd.Timestamp(ts).date()
        rows.append(
            {
                "date": d.isoformat(),
                "open": _f(getattr(c, "open", None)),
                "high": _f(getattr(c, "high", None)),
                "low": _f(getattr(c, "low", None)),
                "close": _f(getattr(c, "close", None)),
                "volume": int(getattr(c, "volume", 0) or 0),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return None
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 持仓导入 (TradeContext.stock_positions)
# ---------------------------------------------------------------------------


def fetch_stock_positions(cfg: dict | None = None) -> list:
    """读取账户股票持仓 (需要凭据有效); 失败抛 RuntimeError."""
    api = _sdk()
    try:
        ctx = api.TradeContext(build_config(cfg))
        resp = ctx.stock_positions()
    except Exception as e:  # noqa: BLE001 - 统一转可读错误
        raise RuntimeError(f"长桥持仓查询失败: {e}") from e
    positions: list = []
    for ch in getattr(resp, "channels", []) or []:
        positions.extend(getattr(ch, "positions", []) or [])
    return positions


def positions_to_rows(positions: list) -> tuple[list[dict], list[str]]:
    """SDK StockPosition 列表 → 导入 rows (零/负数量跳过, 无法映射跳过)."""
    rows: list[dict] = []
    skipped: list[str] = []
    for pos in positions:
        raw = str(getattr(pos, "symbol", "") or "").strip()
        y = longport_to_yahoo(raw)
        if not y:
            skipped.append(f"{raw} 无法映射为内部代码 (市场不支持)")
            continue
        qty = _f(getattr(pos, "quantity", None))
        if not qty or qty <= 0:
            continue
        cost = _f(getattr(pos, "cost_price", None))
        ccy = str(getattr(pos, "currency", "") or "").strip().upper()
        rows.append(
            {
                "symbol": y,
                "type": type_for_symbol(y),
                "quantity": round(qty, 6),
                "avg_cost": round(cost, 6) if cost else None,
                "currency": ccy or None,
            }
        )
    return rows, skipped


# ---------------------------------------------------------------------------
# 自选导入 (QuoteContext.watchlist)
# ---------------------------------------------------------------------------


def fetch_watchlist_groups(cfg: dict | None = None) -> list:
    """读取账户自选分组 (WatchlistGroup 列表); 失败抛 RuntimeError."""
    try:
        return _ctx_call(lambda c: c.watchlist(), cfg=cfg) or []
    except Exception as e:  # noqa: BLE001 - 统一转可读错误
        raise RuntimeError(f"长桥自选查询失败: {e}") from e


def watchlist_to_rows(groups: list) -> tuple[list[dict], list[str]]:
    """WatchlistGroup 列表 → watchlist.json 条目 rows + skipped 原因.

    归一: 长桥 ticker.region → Yahoo 规范代码 (700.HK→0700.HK, 600519.SH→
    600519.SS, G13.SG→G13.SI, AAPL.US→AAPL); 每组一个 list 名, 同代码多组
    的 lists 并集由 save_watchlist 去重合并。无法映射/解析失败默认跳过
    (记入 skipped, 不静默丢弃); watchlist.json 对垃圾代码容忍度低。
    长桥占位代码 (N 前缀打新) 与已退市无接替码的代码同样跳过;
    改码代码自动迁移到接替代码 (symbol_migrations)。
    """
    from .symbols import parse


    rows: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for g in groups:
        name = str(getattr(g, "name", "") or "").strip() or "长桥自选"
        for sec in getattr(g, "securities", []) or []:
            raw = str(getattr(sec, "symbol", "") or "").strip()
            if not raw:
                continue
            if _is_unquoted_placeholder(raw):
                msg = f"{raw} 未挂牌/未定价 (长桥占位代码), 已跳过"
                if msg not in skipped:
                    skipped.append(msg)
                continue
            raw_mapped = longport_to_yahoo(raw)
            if not raw_mapped:
                msg = f"{raw} 无法映射为内部代码 (市场不支持)"
            else:
                # 退市/改码代码自动迁移到接替代码 (无接替码则跳过)
                yahoo, mig = migrate_symbol(raw_mapped)
                if mig and yahoo == raw_mapped:
                    msg = f"{raw} 已退市: {mig}"
                    if msg not in skipped:
                        skipped.append(msg)
                    continue
                try:
                    yahoo = parse(yahoo).yahoo
                except ValueError as e:
                    msg = f"{raw}: {e}"
                else:
                    msg = None
            if msg:
                # 同一代码在多个分组重复出现: 跳过原因只记一次
                if msg not in skipped:
                    skipped.append(msg)
                continue
            note = str(getattr(sec, "name", "") or "").strip()
            wp = _f(getattr(sec, "watched_price", None))
            if wp:
                # 关注价 (当地货币, 长桥 App 里的成本锚): watchlist.json schema 无此键,
                # 并入 note 供参考, 阈值留给用户自己设
                ref = f"长桥关注价 {wp:g}"
                note = f"{note} ({ref})" if note else ref
            if yahoo in seen:
                # 同代码已在先前分组导入过: 只补组名, 不重复出 row
                for r in rows:
                    if r["symbol"] == yahoo and name not in r["lists"]:
                        r["lists"].append(name)
                continue
            seen.add(yahoo)
            rows.append({"symbol": yahoo, "lists": [name], "note": note or None})
    return rows, skipped
