"""EODHD BYOK 接入: 付费数据源 (用户自备 token), 无 token 时不参与源链, 失败自动回落.

EODHD (https://eodhd.com) 与项目内置的免费源定位不同: 它需要用户自己的
API token, 因此所有开关与凭据都放在数据目录的 `eodhd.json` (或环境变量),
不写进 settings.json / 代码, 也不做任何「默认开启」。

取数角色:
  - 历史K线: 默认 primary (在 yfinance 之前; IBKR/长桥仍由 orchestration 前置)
  - 实时行情: 默认 fallback (仅当 yfinance/akshare 失败时兜底), 可改 primary
    —— primary 走 `s=` 批量 (每批 15 个代码), 每个代码仍算 1 API call
  - 基本面 (PB/市值): 默认 fallback (只补 yfinance 缺失字段), 可改 primary

字段与口径:
  - 行情: /api/real-time/{ticker}; close 为最新价, previousClose 为昨收,
    change_p 已是百分数 (0.63 = +0.63%), 不需要再 ×100
  - 基本面: /api/v1.1/fundamentals/{ticker}?filter=Highlights,General;
    Highlights.BookValue → 每股净资产, Highlights.MarketCapitalization → 总市值
  - 历史: /api/eod/{ticker}; 默认用 adjusted_close (拆股+分红复权) 作为 close,
    并把 open/high/low 按同一比例缩放, 保证 K 线连续且不破坏 OHLC 关系;
    config `adjustment: raw` 可切回不复权
  - LSE (.L/.AL) 通常以便士报价: `lse_pence: true` (默认) 会 ÷100 折成英镑;
    英镑计价标的 (如部分 LSE ETF) 用 `lse_gbp_symbols` 列出豁免;
    `.IL` (IOB) 不做便士折算, 与 yfinance 路径一致
  - 代码映射: Yahoo 规范代码 → EODHD `SYMBOL.EXCHANGE`; 未覆盖的市场返回
    None, 源链自动跳过 (不报错、不影响其它市场)
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .paths import TEMPLATES_DIR, data_home
from .symbols import Market, ParsedSymbol, parse as _parse_symbol

if TYPE_CHECKING:  # 仅类型标注; 运行时惰性导入避免 provider 包循环
    from .providers.base import Quote

#: EODHD REST 根地址 (仅测试/代理场景需要改)
DEFAULT_BASE_URL = "https://eodhd.com/api"

#: token 环境变量 (优先级高于 eodhd.json; 便于容器/CI 注入而不落盘)
TOKEN_ENV = "TRACKER_EODHD_TOKEN"
TOKEN_ENV_ALIASES = (TOKEN_ENV, "EODHD_API_TOKEN")
#: 配置文件路径环境变量 (与 IBKR_CONFIG / LONGPORT_CONFIG 同风格)
CONFIG_ENV = "EODHD_CONFIG"
CONFIG_NAME = "eodhd.json"

DEFAULTS: dict = {
    "enabled": True,
    "token": "",
    "base_url": DEFAULT_BASE_URL,
    "timeout": 10.0,
    "priority": "primary",  # 历史K线: primary=先于 yfinance / fallback=最后兜底
    "quotes": "fallback",  # 实时行情: off / fallback / primary
    "fundamentals": "fallback",  # PB/市值: off / fallback=只补缺失 / primary=EODHD 优先
    "adjustment": "adjusted",  # adjusted=拆股+分红复权 / raw=原始价
    "lse_pence": True,  # LSE .L/.AL 是否便士报价 (÷100)
    "lse_gbp_symbols": [],  # 英镑计价的 LSE 标的 (不做便士折算)
    "currency_overrides": {},  # {代码: 币种}, 如 {"KAP.IL": "USD"}
}

# Yahoo 后缀 → EODHD exchange code. 只映射已确认覆盖的交易所; 未列出
# (如 .BE 柏林) 返回 None, 由 yfinance/akshare 兜底。
_EXCHANGE_BY_SUFFIX = {
    "": "US",
    "HK": "HK",
    "L": "LSE",
    "IL": "LSE",
    "AL": "LSE",
    "DE": "XETRA",
    "F": "F",
    "DU": "DU",
    "HM": "HA",
    "MU": "MU",
    "TO": "TO",
    "V": "V",
    "NE": "NEO",
    "AX": "AU",
    "SI": "SG",
    "T": "TSE",
    "KS": "KO",
    "KQ": "KQ",
    "SA": "SA",
}

# 进程内文件缓存: (mtime_ns, size) → 解析结果. 自选 600+ 时源链会反复读
# 同一份配置; 有 mtime 校验, 手改文件无需重启进程即可生效。
_file_cache: dict[Path, tuple[tuple[int, int], dict]] = {}
_file_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 配置读取 / 写入
# ---------------------------------------------------------------------------


def config_path(path: str | Path | None = None) -> Path:
    """eodhd.json 路径: 显式 path > env EODHD_CONFIG > 数据目录/eodhd.json."""
    if path:
        return Path(path).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return data_home() / CONFIG_NAME


def _load_file(p: Path) -> dict:
    """读取 JSON 对象; 缺失/损坏/非对象返回 {} (配置错误不阻断取数)."""
    if not p.exists():
        return {}
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _file_lock:
        hit = _file_cache.get(p)
        if hit is not None and hit[0] == key:
            return dict(hit[1])
    try:
        # utf-8-sig: 兼容编辑器存成的带 BOM 文件
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _file_lock:
        _file_cache[p] = (key, data)
    return dict(data)


def _env_token() -> str | None:
    for name in TOKEN_ENV_ALIASES:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return default


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,;\s]+", v) if x]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        s = str(x).strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def _str_map(v) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, val in v.items():
        s = str(k).strip().upper()
        if s and val is not None and str(val).strip():
            out[s] = str(val).strip().upper()
    return out


def load_config(path: str | Path | None = None) -> dict:
    """读取并校验 EODHD 配置 (内置默认 → eodhd.json → env token)."""
    p = config_path(path)
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in _load_file(p).items() if not str(k).startswith("_")})
    env_token = _env_token()
    if env_token:
        cfg["token"] = env_token
        cfg["_token_source"] = "env"
    else:
        cfg["_token_source"] = "config" if str(cfg.get("token") or "").strip() else None
    cfg["token"] = str(cfg.get("token") or "").strip()
    cfg["enabled"] = _as_bool(cfg.get("enabled"), True)
    base = str(cfg.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    cfg["base_url"] = base or DEFAULT_BASE_URL
    try:
        timeout = float(cfg.get("timeout") or DEFAULTS["timeout"])
    except (TypeError, ValueError):
        timeout = DEFAULTS["timeout"]
    cfg["timeout"] = min(60.0, max(1.0, timeout))
    priority = str(cfg.get("priority") or "primary").strip().lower()
    cfg["priority"] = priority if priority in ("primary", "fallback") else "primary"
    quotes = str(cfg.get("quotes") or "fallback").strip().lower()
    cfg["quotes"] = quotes if quotes in ("off", "fallback", "primary") else "fallback"
    fundamentals = str(cfg.get("fundamentals") or "fallback").strip().lower()
    cfg["fundamentals"] = (
        fundamentals if fundamentals in ("off", "fallback", "primary") else "fallback"
    )
    adjustment = str(cfg.get("adjustment") or "adjusted").strip().lower()
    cfg["adjustment"] = adjustment if adjustment in ("adjusted", "raw") else "adjusted"
    cfg["lse_pence"] = _as_bool(cfg.get("lse_pence"), True)
    cfg["lse_gbp_symbols"] = _str_list(cfg.get("lse_gbp_symbols"))
    cfg["currency_overrides"] = _str_map(cfg.get("currency_overrides"))
    cfg["_path"] = str(p)
    return cfg


def reset() -> None:
    """清空配置缓存 (设置页写入 / 测试替换环境变量后调用)."""
    with _file_lock:
        _file_cache.clear()


def available(cfg: dict | None = None) -> bool:
    """enabled 且 token 非空时才真正参与源链."""
    cfg = cfg or load_config()
    return bool(cfg.get("enabled") and cfg.get("token"))


def unavailable_reason(cfg: dict | None = None) -> str:
    """不可用原因 (仅用于状态展示; 可用时返回空串)."""
    cfg = cfg or load_config()
    if not cfg.get("enabled"):
        return f"{CONFIG_NAME} 中 enabled=false"
    if not cfg.get("token"):
        return f"未配置 token: 在 {CONFIG_NAME} 填 token, 或设 env {TOKEN_ENV}"
    return ""


def quote_primary(cfg: dict | None = None) -> bool:
    """实时行情是否 EODHD 优先 (orchestration 据此跳过 yfinance 批量)."""
    cfg = cfg or load_config()
    return available(cfg) and cfg.get("quotes") == "primary"


def fundamentals_mode(cfg: dict | None = None) -> str:
    """基本面 (PB/市值) 使用模式: off / fallback / primary (未配置 token → off)."""
    cfg = cfg or load_config()
    if not available(cfg):
        return "off"
    return str(cfg.get("fundamentals") or "fallback")


def cache_signature(cfg: dict | None = None) -> str:
    """进入 K 线磁盘缓存键的源签名: 切换角色/复权口径后不复用旧数据."""
    cfg = cfg or load_config()
    if not available(cfg):
        return ""
    sig = (
        f"|eodhd={cfg['priority']}q{cfg['quotes']}"
        f"a{int(cfg['adjustment'] == 'adjusted')}p{int(bool(cfg['lse_pence']))}"
    )
    # 币种覆盖会改变 LSE 便士判定, 豁免表直接影响历史价格: 有内容时进签名,
    # 避免改了配置仍命中旧值 (空表/空映射是默认值, 不必进键)。
    if cfg["lse_gbp_symbols"] or cfg["currency_overrides"]:
        import hashlib

        extra = json.dumps(
            {
                "g": sorted(cfg["lse_gbp_symbols"]),
                "o": dict(sorted(cfg["currency_overrides"].items())),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        sig += "|x" + hashlib.sha1(extra.encode("utf-8")).hexdigest()[:8]
    return sig


def _write_config(patch: dict, path: str | Path | None = None) -> Path:
    """原子写 eodhd.json (0600): 只更新 patch 中的键, 保留 token/自定义键."""
    p = config_path(path)
    if p.exists():
        data = _load_file(p)
    else:
        # 首次经设置页创建时带上模板的字段说明, 避免只落一个裸 enabled 键
        tpl = TEMPLATES_DIR / "eodhd.example.json"
        data = _load_file(tpl) if tpl.exists() else {}
    data.update(patch)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # 含 token: 仅属主可读写
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reset()
    return p


def set_enabled(value: bool, path: str | Path | None = None) -> Path:
    """设置页开关: 只改 enabled, 不碰 token 与其它字段."""
    return _write_config({"enabled": bool(value)}, path)


def set_token(token: str, path: str | Path | None = None) -> Path:
    """写入/清空 token (空串 = 删除). 文件权限保持 0600."""
    return _write_config({"token": str(token or "").strip()}, path)


def status(path: str | Path | None = None) -> dict:
    """配置与可用性快照 (不联网; token 只输出掩码)."""
    cfg = load_config(path)
    token = cfg.get("token") or ""
    return {
        "ok": True,
        "config_path": cfg["_path"],
        "config_exists": Path(cfg["_path"]).exists(),
        "enabled": bool(cfg["enabled"]),
        "available": available(cfg),
        "reason": unavailable_reason(cfg),
        "token_present": bool(token),
        "token_masked": _mask(token),
        "token_source": cfg.get("_token_source"),
        "token_env": TOKEN_ENV,
        "base_url": cfg["base_url"],
        "priority": cfg["priority"],
        "quotes": cfg["quotes"],
        "fundamentals": cfg["fundamentals"],
        "adjustment": cfg["adjustment"],
        "lse_pence": bool(cfg["lse_pence"]),
        "lse_gbp_symbols": list(cfg["lse_gbp_symbols"]),
        "currency_overrides": dict(cfg["currency_overrides"]),
    }


def _mask(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}…{token[-4:]}"


# ---------------------------------------------------------------------------
# 代码映射
# ---------------------------------------------------------------------------


def yahoo_to_eodhd(symbol: str) -> str | None:
    """Yahoo 规范代码 → EODHD ticker; 不支持的市场返回 None.

    非全球股票域 (cn/crypto/index) 一律返回 None: EODHD 对 A 股/加密也有
    覆盖, 但本项目这些域已有更合适的主源, 首版只把 EODHD 接在全球股票域。
    """
    try:
        p = _parse_symbol(symbol)
    except ValueError:
        return None
    if p.type != "global":
        return None
    suffix = p.yahoo.rsplit(".", 1)[1].upper() if "." in p.yahoo else ""
    exchange = _EXCHANGE_BY_SUFFIX.get(suffix)
    if not exchange:
        return None
    code = p.yahoo.rsplit(".", 1)[0] if suffix else p.yahoo
    return f"{code}.{exchange}"


def _pence_symbol(p: ParsedSymbol, cfg: dict) -> bool:
    """LSE 便士报价判定 (与 yfinance 路径的 GBp/GBX 处理保持一致)."""
    if p.market is not Market.GB:
        return False
    if p.yahoo.upper().endswith(".IL"):
        return False
    # 显式币种覆盖 = 已声明该标的不是「LSE 便士」口径, 不再 ÷100
    if p.yahoo.upper() in cfg.get("currency_overrides", {}):
        return False
    if p.yahoo.upper() in cfg.get("lse_gbp_symbols", []):
        return False
    return bool(cfg.get("lse_pence", True))


def _currency(p: ParsedSymbol, cfg: dict) -> str:
    return cfg.get("currency_overrides", {}).get(p.yahoo.upper(), p.currency)


def _quote_currency_and_price(
    p: ParsedSymbol, price: float, prev: float | None, cfg: dict
) -> tuple[float, float | None, str]:
    if _pence_symbol(p, cfg):
        price = float(price) / 100
        if prev is not None:
            prev = float(prev) / 100
        return price, prev, "GBP"
    return float(price), (float(prev) if prev is not None else None), _currency(p, cfg)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _scrub(text: str, token: str) -> str:
    """错误信息脱敏: requests 的异常串里可能带完整 URL (含 api_token)."""
    s = str(text)
    if token:
        s = s.replace(token, "***")
    return re.sub(r"(api_token=)[^&\s\"']+", r"\1***", s)


def _http_json(path: str, params: dict, cfg: dict):
    """GET JSON; 4xx/5xx 转可读 RuntimeError, 且绝不把 token 带进异常."""
    try:
        import requests
    except ImportError as e:  # pragma: no cover - requests 是通行依赖
        raise RuntimeError("未安装 requests, 无法访问 EODHD") from e
    token = str(cfg.get("token") or "")
    url = f"{cfg['base_url'].rstrip('/')}/{path.lstrip('/')}"
    try:
        resp = requests.get(
            url, params={**params, "api_token": token}, timeout=cfg["timeout"]
        )
    except Exception as e:  # noqa: BLE001 - 网络异常统一转可读错误
        raise RuntimeError(f"EODHD 请求失败: {_scrub(e, token)}") from e
    code = int(getattr(resp, "status_code", 0) or 0)
    hints = {
        401: "认证失败 (401): token 无效或缺失",
        403: "无权限 (403): 当前套餐未覆盖该接口/标的",
        404: "未收录该代码 (404): 检查 EODHD ticker 映射",
        429: "请求过多 (429): 触发 EODHD 限流, 稍后重试",
    }
    if code in hints:
        raise RuntimeError(f"EODHD {hints[code]}")
    if code >= 400:
        body = _scrub(getattr(resp, "text", "")[:200], token)
        raise RuntimeError(f"EODHD HTTP {code}: {body}".strip())
    try:
        return resp.json()
    except ValueError as e:
        body = _scrub(getattr(resp, "text", "")[:200], token)
        raise RuntimeError(f"EODHD 返回非 JSON: {body}".strip()) from e


def _positive(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _number(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# 行情 / 历史
# ---------------------------------------------------------------------------


def _quote_from_row(p: ParsedSymbol, data: dict, cfg: dict) -> Quote | None:
    """单条 EODHD 行情 JSON → Quote; 无有效价格返回 None."""
    from .providers.base import Quote

    if not isinstance(data, dict):
        return None
    price = (
        _positive(data.get("close"))
        or _positive(data.get("open"))
        or _positive(data.get("previousClose"))
    )
    if price is None:
        return None
    prev = _positive(data.get("previousClose"))
    price, prev, currency = _quote_currency_and_price(p, price, prev, cfg)
    chg = _number(data.get("change_p"))
    if chg is None and prev is not None and prev > 0:
        chg = (price / prev - 1) * 100
    return Quote(
        symbol=p.yahoo,
        name=None,
        price=price,
        prev_close=prev,
        change_pct=float(chg) if chg is not None else None,
        currency=currency,
        source="eodhd",
    )


def quote(symbol: str | ParsedSymbol, path: str | Path | None = None):
    """EODHD 实时 (15-20 分钟延迟) 行情 → tracker.providers.base.Quote."""
    p = symbol if isinstance(symbol, ParsedSymbol) else _parse_symbol(symbol)
    cfg = load_config(path)
    if not available(cfg):
        raise RuntimeError(f"EODHD 不可用: {unavailable_reason(cfg)}")
    ticker = yahoo_to_eodhd(p.yahoo)
    if not ticker:
        raise RuntimeError(f"{p.yahoo}: EODHD 不支持该交易所")
    data = _http_json(f"real-time/{ticker}", {"fmt": "json"}, cfg)
    if isinstance(data, list):
        data = data[0] if data else {}
    q = _quote_from_row(p, data, cfg)
    if q is None:
        raise RuntimeError(f"{p.yahoo}: EODHD 无有效价格")
    return q


#: 批量行情单次请求的代码数 (EODHD 建议 s= 传 15-20 个; 每个代码仍算 1 call)
_QUOTE_BATCH = 15
#: 批间退让 (s): 避免密集一批打满每分钟限额
_QUOTE_BATCH_PAUSE = 0.2


def quotes_batch(
    parsed: list[ParsedSymbol], path: str | Path | None = None
) -> tuple[dict[str, Quote], list[str]]:
    """批量实时行情 (EODHD `s=` 参数): 一次请求最多 15 个代码.

    返回 ({yahoo: Quote}, errors)。批量只影响延迟, 不省调用次数 (EODHD 按
    ticker 计费); 未覆盖/解析失败的代码由调用方逐代码源链继续兜底。
    连续两批失败 (token 失效/限流) 直接停止, 不把剩余批次全部撞一遍。
    """
    cfg = load_config(path)
    if not available(cfg):
        return {}, [f"EODHD 不可用: {unavailable_reason(cfg)}"]
    mapping: dict[str, ParsedSymbol] = {}
    for p in parsed:
        ticker = yahoo_to_eodhd(p.yahoo)
        if ticker:
            mapping[ticker] = p
    if not mapping:
        return {}, []
    out: dict[str, Quote] = {}
    errors: list[str] = []
    tickers = list(mapping)
    fail_streak = 0
    for start in range(0, len(tickers), _QUOTE_BATCH):
        if start:
            time.sleep(_QUOTE_BATCH_PAUSE)
        chunk = tickers[start : start + _QUOTE_BATCH]
        params: dict[str, object] = {"fmt": "json"}
        if len(chunk) > 1:
            params["s"] = ",".join(chunk[1:])
        try:
            data = _http_json(f"real-time/{chunk[0]}", params, cfg)
        except Exception as e:  # noqa: BLE001 - 批量失败交给调用方兜底
            errors.append(str(e))
            fail_streak += 1
            if fail_streak >= 2:
                break
            continue
        fail_streak = 0
        rows = data if isinstance(data, list) else [data]
        got = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("error") or row.get("message"):
                errors.append(
                    f"EODHD 批量行情错误: {row.get('error') or row.get('message')}"
                )
                continue
            code = str(row.get("code") or "").strip().upper()
            p = mapping.get(code)
            if p is None:
                continue
            q = _quote_from_row(p, row, cfg)
            if q is not None:
                out[p.yahoo] = q
                got += 1
        if got == 0:
            errors.append("EODHD 批量行情无有效结果")
    return out, errors


def history(
    symbol: str | ParsedSymbol,
    start_date: str,
    end_date: str | None = None,
    path: str | Path | None = None,
) -> pd.DataFrame:
    """EODHD 日线 → DataFrame(date/open/high/low/close/volume, 升序)."""
    p = symbol if isinstance(symbol, ParsedSymbol) else _parse_symbol(symbol)
    cfg = load_config(path)
    if not available(cfg):
        raise RuntimeError(f"EODHD 不可用: {unavailable_reason(cfg)}")
    ticker = yahoo_to_eodhd(p.yahoo)
    if not ticker:
        raise RuntimeError(f"{p.yahoo}: EODHD 不支持该交易所")
    params = {"fmt": "json", "from": start_date, "period": "d", "order": "a"}
    if end_date:
        params["to"] = end_date
    data = _http_json(f"eod/{ticker}", params, cfg)
    if isinstance(data, dict):
        # 错误响应 (部分套餐/网关会返回 {"error": "..."}) 统一转可读错误
        msg = data.get("error") or data.get("message")
        if msg:
            raise RuntimeError(f"{p.yahoo}: EODHD 历史数据错误: {msg}")
        data = [data]
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            f"{p.yahoo}: EODHD 无历史数据 ({start_date}~{end_date or 'today'})"
        )
    df = pd.DataFrame(data)
    if "date" not in df or "close" not in df:
        raise RuntimeError(f"{p.yahoo}: EODHD 历史数据缺少 date/close 字段")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "adjusted_close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # 复权: adjusted_close 是拆股+分红复权后的收盘价; 同比例缩放
    # open/high/low, K 线在拆股日不会出现假跳空 (且不破坏 high/low 关系)。
    if cfg["adjustment"] == "adjusted" and "adjusted_close" in df:
        raw_close = df["close"]
        adj = df["adjusted_close"]
        # adjusted_close 偶发缺失/为 0 时逐行回退原始 close (不要整行丢掉)
        valid_adj = adj.where(adj > 0)
        factor = (valid_adj / raw_close.where(raw_close > 0)).fillna(1.0)
        for col in ("open", "high", "low"):
            if col in df:
                df[col] = df[col] * factor
        df["close"] = valid_adj.fillna(raw_close)
    if _pence_symbol(p, cfg):
        for col in ("open", "high", "low", "close"):
            if col in df:
                df[col] = df[col] / 100
    keep = ["date", "open", "high", "low", "close", "volume"]
    for col in keep:
        if col not in df:
            df[col] = float("nan")
    df = df[keep]
    df = df[
        df["close"].map(
            lambda v: bool(pd.notna(v) and math.isfinite(float(v)) and v > 0)
        )
    ]
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"{p.yahoo}: EODHD 无有效历史数据")
    return df


def fundamentals(symbol: str | ParsedSymbol, path: str | Path | None = None) -> dict:
    """EODHD 基本面 (PB 用的每股净资产 + 总市值, 当地货币).

    只取 Highlights/General 两个 section (filter= 减少载荷), 1 API call。
    字段缺失时返回 None 而不是抛错 (与 tracker.fundamentals 的降级语义一致)。
    """
    p = symbol if isinstance(symbol, ParsedSymbol) else _parse_symbol(symbol)
    cfg = load_config(path)
    if not available(cfg):
        raise RuntimeError(f"EODHD 不可用: {unavailable_reason(cfg)}")
    ticker = yahoo_to_eodhd(p.yahoo)
    if not ticker:
        raise RuntimeError(f"{p.yahoo}: EODHD 不支持该交易所")
    data = _http_json(
        f"v1.1/fundamentals/{ticker}",
        {"fmt": "json", "filter": "Highlights,General"},
        cfg,
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"{p.yahoo}: EODHD 基本面响应格式异常")
    if data.get("error") or data.get("message"):
        raise RuntimeError(
            f"{p.yahoo}: EODHD 基本面错误: {data.get('error') or data.get('message')}"
        )
    hl = data.get("Highlights") if isinstance(data.get("Highlights"), dict) else {}
    gen = data.get("General") if isinstance(data.get("General"), dict) else {}
    return {
        "symbol": p.yahoo,
        "book_value": _number(hl.get("BookValue")),
        "market_cap": _positive(hl.get("MarketCapitalization")),
        "currency": str(gen.get("CurrencyCode") or "").strip() or None,
        "name": str(gen.get("Name") or "").strip() or None,
        "source": "eodhd",
    }


def probe(symbol: str, path: str | Path | None = None) -> dict:
    """连通性自检: 一次行情 + 最近 7 天历史; 供 `tracker eodhd test` 使用."""
    from datetime import date, timedelta

    cfg = load_config(path)
    out = {
        "symbol": symbol,
        "available": available(cfg),
        "reason": unavailable_reason(cfg),
        "quote": None,
        "history": None,
        "fundamentals": None,
        "errors": [],
    }
    if not available(cfg):
        out["errors"].append(out["reason"])
        return out
    try:
        q = quote(symbol, path)
        out["quote"] = {
            "price": q.price,
            "prev_close": q.prev_close,
            "change_pct": q.change_pct,
            "currency": q.currency,
            "source": q.source,
        }
    except Exception as e:  # noqa: BLE001 - 自检报告所有失败项
        out["errors"].append(f"行情: {e}")
    try:
        df = history(
            symbol,
            (date.today() - timedelta(days=7)).isoformat(),
            date.today().isoformat(),
            path,
        )
        out["history"] = {
            "rows": int(len(df)),
            "first": str(df["date"].iloc[0].date()),
            "last": str(df["date"].iloc[-1].date()),
            "last_close": float(df["close"].iloc[-1]),
        }
    except Exception as e:  # noqa: BLE001 - 自检报告所有失败项
        out["errors"].append(f"历史: {e}")
    try:
        f = fundamentals(symbol, path)
        out["fundamentals"] = {
            "book_value": f["book_value"],
            "market_cap": f["market_cap"],
            "currency": f["currency"],
            "source": f["source"],
        }
    except Exception as e:  # noqa: BLE001 - 自检报告所有失败项
        out["errors"].append(f"基本面: {e}")
    return out
