"""链上钱包历史余额: 按转账记录回放每个自然日的余额.

与 ``tracker.wallet`` 的「当前余额」查询互补:

- ``wallet`` 只回答「现在有多少」;
- 本模块回答「过去每天有多少」—— 先取当前余额作为锚点, 再取窗口内的
  链上转账 / 交易 / 手续费, 从今天往前反推每天收盘时的余额。因此不需要
  归档节点, 也不需要从创世块扫链。

支持来源:

==========  =========================================================
链          默认来源 (零 API Key)
==========  =========================================================
eth         Routescan (公开, 无需 key), Blockscout 兜底
avalanche   Snowtrace, Routescan 兜底
tron        TronGrid ``/v1/accounts/{addr}/transactions`` + ``trc20``
solana      公共 JSON-RPC (getSignaturesForAddress + getTransaction)
polygon     Blockscout (若可访问) / 需 Etherscan V2 key
arbitrum    Blockscout (若可访问) / 需 Etherscan V2 key
bsc         需 Etherscan V2 key 或自建 ``explorers`` 地址
==========  =========================================================

Polygon / Arbitrum / BSC 的免费公共浏览器接口并不稳定或需要 key。可在
``wallet_history.json`` 配置 ``explorer_key`` (Etherscan V2, 或用环境变量
``TRACKER_EXPLORER_KEY``), 也可用 ``explorers`` / ``blockscout_urls`` 指向
自建或其它兼容服务。未配置时只影响这三条链, 其余四条链开箱可用。

口径与限制:

- 余额按 **UTC 自然日收盘** 回放; 当前余额取查询时刻的链上余额。
- EVM 会同时取普通交易、内部交易与 ERC-20 转账, 并扣除转出方手续费;
  TRON 取 TRX 转账、TRC-20 转账与合约手续费; Solana 直接使用每笔交易的
  pre/post balances (含手续费与租金变化)。
- 质押 / 解冻中的 TRX 不计入历史余额 (TRON 质押事件不是普通转账)。
- 转账手续费、rebasing / fee-on-transfer 代币、以及浏览器索引缺失可能
  让回放与真实余额有偏差; 结果只用于看趋势, 不作为审计口径。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from . import config
from .paths import TEMPLATES_DIR, data_home

CONFIG_NAME = "wallet_history.json"
CONFIG_ENV = "TRACKER_WALLET_HISTORY_CONFIG"
EXPLORER_KEY_ENV = "TRACKER_EXPLORER_KEY"

DEFAULTS: dict[str, Any] = {
    "explorer_key": "",
    "explorers": {},
    "blockscout_urls": {},
    "max_pages": 40,
    "max_transactions": 1200,
    "timeout": 15.0,
}

#: EVM chain → Etherscan V2 chainid (Etherscan 兼容接口)
_EVM_CHAIN_ID = {
    "eth": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "avalanche": 43114,
}

#: Routescan 公开 Data API 目前实际索引的公网链
_ROUTESCAN_CHAINS = {"eth": 1, "avalanche": 43114}

#: Blockscout 公开实例 (部分网络会触发 Cloudflare 人机校验, 失败自动换源)
_DEFAULT_BLOCKSCOUT = {
    "eth": "https://eth.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
}

_ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
_SNOWTRACE = "https://api.snowtrace.io/api"

#: 进程内收集需要脱敏的密钥 (浏览器 API key), 错误信息里统一替换为 ***
_SECRETS: set[str] = set()


def _scrub(text: Any) -> str:
    """错误信息脱敏: 不把 API key / apikey 查询参数带进 CLI/UI 输出."""
    s = str(text)
    for secret in _SECRETS:
        if secret:
            s = s.replace(secret, "***")
    return re.sub(r"(apikey=)[^&\s\"']+", r"\1***", s, flags=re.IGNORECASE)


#: 兼容旧代码/测试的别名
SUPPORTED_CHAINS = ("eth", "bsc", "polygon", "arbitrum", "avalanche", "tron", "solana")


class WalletHistoryError(RuntimeError):
    """链上历史查询失败 (网络/接口/配置), 由 CLI/UI 转成可读提示."""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def config_path(explicit: str | None = None):
    """钱包历史配置文件路径: 显式参数 > 环境变量 > 数据目录 > 包内模板."""
    return config.resolve_config_path(
        explicit,
        CONFIG_ENV,
        data_home() / CONFIG_NAME,
        TEMPLATES_DIR / "wallet_history.example.json",
    )


def load_config(path: str | None = None) -> dict[str, Any]:
    """读取并归一化配置; 缺失/损坏按默认值处理 (可选数据源不阻断主流程)."""
    p = config_path(path)
    raw = config.load_json_object(p)
    cfg = config.merge_config(
        DEFAULTS,
        raw,
        deep_keys=("explorers", "blockscout_urls"),
    )
    key = os.environ.get(EXPLORER_KEY_ENV)
    cfg["explorer_key"] = str(
        key if key is not None else cfg.get("explorer_key") or ""
    ).strip()
    _SECRETS.clear()
    if cfg["explorer_key"]:
        _SECRETS.add(cfg["explorer_key"])
    cfg["explorers"] = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (cfg.get("explorers") or {}).items()
        if str(v).strip()
    }
    cfg["blockscout_urls"] = {
        str(k).strip().lower(): str(v).strip().rstrip("/")
        for k, v in (cfg.get("blockscout_urls") or {}).items()
        if str(v).strip()
    }
    try:
        cfg["max_pages"] = max(1, min(int(cfg.get("max_pages") or 40), 500))
    except (TypeError, ValueError):
        cfg["max_pages"] = 40
    try:
        cfg["max_transactions"] = max(
            1, min(int(cfg.get("max_transactions") or 1200), 10000)
        )
    except (TypeError, ValueError):
        cfg["max_transactions"] = 1200
    try:
        cfg["timeout"] = max(3.0, min(float(cfg.get("timeout") or 15.0), 120.0))
    except (TypeError, ValueError):
        cfg["timeout"] = 15.0
    cfg["_path"] = str(p)
    return cfg


def reset() -> None:
    """清空配置缓存 (测试替换环境变量/写配置后调用)."""
    config.clear_cache()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    """钱包里的一项资产 (原生币或代币)."""

    key: str
    symbol: str
    decimals: int
    kind: str  # native / token
    chain: str


@dataclass(frozen=True)
class Transfer:
    """一笔对钱包余额的带符号变化 (单位: 整个代币, 不是最小单位)."""

    ts: int
    asset_key: str
    delta: float
    txid: str = ""


@dataclass
class Ledger:
    """某个地址的历史采集结果 (当前余额 + 窗口内转账)."""

    chain: str
    address: str
    assets: dict[str, Asset] = field(default_factory=dict)
    current: dict[str, float] = field(default_factory=dict)
    #: 浏览器给出的当前 USD 估值 (可选), 用于在资产很多时优先保留大头
    current_usd: dict[str, float] = field(default_factory=dict)
    transfers: list[Transfer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    truncated: bool = False
    #: 值得继续估值/展示的资产 key。空投垃圾币常把链上钱包塞满成千上万个
    #: token account; 逐个取历史价会拖死数据源, 因此只信任原生币、tokenlist
    #: 内的主流币、转账记录里真实出现过的币, 以及浏览器给出正 USD 价值的币。
    trusted: set[str] = field(default_factory=set)

    def add_asset(self, asset: Asset) -> Asset:
        old = self.assets.get(asset.key)
        # 同一代币可能先由转账记录 (可能缺 symbol) 建立, 后被持仓接口补全:
        # 保留更完整的元数据, 避免后续估值拿不到 symbol/decimals。
        if old is None or (not old.symbol and asset.symbol):
            self.assets[asset.key] = asset
            return asset
        return old

    def trust(self, asset: Asset) -> None:
        self.add_asset(asset)
        self.trusted.add(asset.key)

    def add_transfer(self, ts: int, asset: Asset, delta: float, txid: str = "") -> None:
        if not math.isfinite(delta) or delta == 0:
            return
        self.trust(asset)
        self.transfers.append(
            Transfer(ts=int(ts), asset_key=asset.key, delta=float(delta), txid=txid)
        )

    def set_current(
        self, asset: Asset, amount: float, usd_value: float | None = None
    ) -> None:
        if not math.isfinite(amount):
            return
        self.add_asset(asset)
        self.current[asset.key] = float(amount)
        if usd_value is not None and math.isfinite(float(usd_value)):
            self.current_usd[asset.key] = float(usd_value)


def _known_contracts(chain: str, tokenlist: str | None) -> set[str]:
    """tokenlist 内合约集合 (用于判断哪些持仓值得取价)."""
    try:
        from . import wallet

        data = wallet._load_tokenlist(tokenlist).get(chain, {})
    except (ValueError, FileNotFoundError, OSError):
        return set()
    return {str(k) for k in data}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _http_json(
    url: str,
    params: dict | None = None,
    *,
    timeout: float = 15.0,
    headers: dict | None = None,
) -> Any:
    """GET JSON; 网络/HTTP/JSON 错误统一转 WalletHistoryError."""
    try:
        import requests
    except ImportError as e:  # pragma: no cover - requests 是通行依赖
        raise WalletHistoryError("未安装 requests, 无法访问链上浏览器") from e
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = requests.get(
            url,
            params=clean,
            timeout=timeout,
            headers={"User-Agent": "madeleine-tracker", **(headers or {})},
        )
    except Exception as e:  # noqa: BLE001 - 网络异常统一转可读错误
        raise WalletHistoryError(f"请求失败: {_scrub(e)}") from e
    code = int(getattr(resp, "status_code", 0) or 0)
    if code >= 400:
        body = _scrub(getattr(resp, "text", "") or "")[:200]
        if code == 403:
            raise WalletHistoryError(
                "HTTP 403 (浏览器接口被 Cloudflare 拦截或需要 API Key)"
            )
        raise WalletHistoryError(f"HTTP {code}: {body}".strip())
    try:
        return resp.json()
    except ValueError as e:
        body = _scrub(getattr(resp, "text", "") or "")[:200]
        raise WalletHistoryError(f"返回非 JSON: {body}".strip()) from e


def _parse_ts(value: Any) -> int | None:
    """时间戳归一为 epoch 秒; 支持秒/毫秒数字与 ISO 字符串."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v):
            return None
        return int(v / 1000) if abs(v) > 1e11 else int(v)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _day_start_ts(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _day_end_ts(day: date) -> int:
    """day 当天最后一秒 (含)."""
    return _day_start_ts(day + timedelta(days=1)) - 1


def _utc_day(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _raw_to_amount(raw: Any, decimals: Any) -> float | None:
    """最小单位整数 → 代币数量; 畸形 raw/decimals 返回 None."""
    try:
        amount = int(str(raw).strip())
        dec = max(0, int(decimals))
    except (TypeError, ValueError):
        return None
    if dec > 36:
        return None
    return amount / (10**dec)


def _short(s: str, n: int = 8) -> str:
    s = str(s or "")
    return s if len(s) <= n else f"{s[:n]}…"


def _validate_chain_address(chain: str, address: str) -> tuple[str, str]:
    """链名/地址校验复用 tracker.wallet (七条链的地址规则已在那边收口)."""
    from . import wallet

    chain_l = str(chain or "").strip().lower()
    if chain_l not in wallet._SUPPORTED_CHAINS:
        raise WalletHistoryError(
            f"不支持的链: {chain_l}. 支持: {', '.join(sorted(wallet._SUPPORTED_CHAINS))}"
        )
    addr = wallet._validate_address(address, chain_l)
    return chain_l, addr


def _native_asset(chain: str) -> Asset:
    from . import wallet

    info = wallet._CHAIN_CONFIG[chain]
    return Asset(
        key="native",
        symbol=str(info["native_symbol"]),
        decimals=int(info["native_decimals"]),
        kind="native",
        chain=chain,
    )


def _token_key(chain: str, contract: str) -> str:
    c = str(contract or "").strip()
    # EVM 地址大小写不敏感 (Routescan 返回 checksum 形式); TRON/Solana 区分大小写
    if chain in _EVM_CHAIN_ID:
        return c.lower()
    return c


def _token_asset(
    chain: str,
    contract: str,
    symbol: Any,
    decimals: Any,
    *,
    fallback: str | None = None,
) -> Asset:
    key = _token_key(chain, contract)
    sym = str(symbol or "").strip()
    if not sym:
        sym = fallback or _short(key, 8)
    try:
        dec = max(0, int(decimals))
    except (TypeError, ValueError):
        dec = 0
    return Asset(key=key, symbol=sym, decimals=dec, kind="token", chain=chain)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def fetch_ledger(
    chain: str,
    address: str,
    start: date,
    end: date,
    *,
    cfg: dict | None = None,
    tokenlist: str | None = None,
    refresh: bool = False,
) -> Ledger:
    """采集某地址在 [start, end] 窗口内的余额与转账.

    当前余额取查询时刻的链上余额; 窗口内的转账用于从今天向前反推历史余额。
    网络失败抛 :class:`WalletHistoryError`; 部分接口失败记入 ``notes``。
    """
    del refresh  # 预留: 未来可加磁盘缓存; 现在每次都实时查询
    cfg = cfg or load_config()
    chain_l, addr = _validate_chain_address(chain, address)
    start = date.fromisoformat(start.isoformat())
    end = date.fromisoformat(end.isoformat())
    if start > end:
        raise WalletHistoryError("开始日期不能晚于结束日期")
    start_ts, end_ts = _day_start_ts(start), _day_end_ts(end)
    if chain_l == "tron":
        return _fetch_tron(addr, start_ts, end_ts, cfg, tokenlist)
    if chain_l == "solana":
        return _fetch_solana(addr, start_ts, end_ts, cfg, tokenlist)
    return _fetch_evm(chain_l, addr, start_ts, end_ts, cfg, tokenlist)


def reconstruct_daily_balances(
    ledger: Ledger,
    start: date,
    end: date,
) -> tuple[list[date], dict[str, list[float]], list[str]]:
    """从当前余额反推每天收盘余额.

    返回 (自然日列表, {资产 key: 与日期对齐的余额列表}, 问题列表)。

    原理: ``当前余额 = 窗口起点余额 + 窗口内净转账``。从最后一天往前,
    每退一天就把当天的净变化减掉, 得到该天收盘余额。这样即使钱包在窗口
    开始前已有余额, 也不需要再查更早的历史。
    """
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    days: list[date] = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    index = {day: i for i, day in enumerate(days)}
    start_ts, end_ts = _day_start_ts(start), _day_end_ts(end)

    deltas: dict[str, list[float]] = {}
    for asset in ledger.assets.values():
        deltas.setdefault(asset.key, [0.0] * len(days))
    for key in ledger.current:
        deltas.setdefault(key, [0.0] * len(days))
    for tr in ledger.transfers:
        if tr.ts < start_ts or tr.ts > end_ts:
            continue
        day = _utc_day(tr.ts)
        pos = index.get(day)
        if pos is None:
            continue
        deltas.setdefault(tr.asset_key, [0.0] * len(days))[pos] += float(tr.delta)

    balances: dict[str, list[float]] = {}
    issues: list[str] = []
    for key, changes in deltas.items():
        asset = ledger.assets.get(key)
        label = asset.symbol if asset else _short(key)
        running = float(ledger.current.get(key) or 0.0)
        vals = [0.0] * len(days)
        for i in range(len(days) - 1, -1, -1):
            vals[i] = running
            running -= changes[i]
        if any(v < -1e-9 for v in vals):
            issues.append(
                f"{label}: 回放出现负余额 (可能缺失转账/手续费记录), 已截到 0"
            )
            vals = [max(0.0, v) for v in vals]
        balances[key] = vals
    return days, balances, issues


# ---------------------------------------------------------------------------
# EVM: 公共入口 + 源链切换
# ---------------------------------------------------------------------------


def _fetch_evm(
    chain: str,
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    tokenlist: str | None = None,
) -> Ledger:
    """按可用性依次尝试 EVM 浏览器源, 任一完整成功即返回."""
    specs: list[tuple[str, Callable[[], Ledger]]] = []
    custom = (cfg.get("explorers") or {}).get(chain)
    if custom:
        specs.append(
            (
                "自定义浏览器",
                lambda u=custom: _fetch_evm_etherscan_like(
                    u,
                    chain,
                    address,
                    start_ts,
                    end_ts,
                    cfg,
                    api_key=cfg.get("explorer_key") or None,
                    label="自定义浏览器",
                    tokenlist=tokenlist,
                ),
            )
        )
    if chain in _ROUTESCAN_CHAINS:
        specs.append(
            (
                "Routescan",
                lambda cid=_ROUTESCAN_CHAINS[chain]: _fetch_evm_routescan(
                    cid, chain, address, start_ts, end_ts, cfg, tokenlist
                ),
            )
        )
    if chain == "avalanche":
        specs.append(
            (
                "Snowtrace",
                lambda: _fetch_evm_etherscan_like(
                    _SNOWTRACE,
                    chain,
                    address,
                    start_ts,
                    end_ts,
                    cfg,
                    label="Snowtrace",
                    tokenlist=tokenlist,
                ),
            )
        )
    bs_url = (cfg.get("blockscout_urls") or {}).get(chain) or _DEFAULT_BLOCKSCOUT.get(
        chain
    )
    if bs_url:
        specs.append(
            (
                "Blockscout",
                lambda u=bs_url: _fetch_evm_blockscout(
                    u, chain, address, start_ts, end_ts, cfg, tokenlist
                ),
            )
        )
    if cfg.get("explorer_key"):
        specs.append(
            (
                "Etherscan V2",
                lambda: _fetch_evm_etherscan_like(
                    _ETHERSCAN_V2,
                    chain,
                    address,
                    start_ts,
                    end_ts,
                    cfg,
                    api_key=cfg["explorer_key"],
                    chain_id=_EVM_CHAIN_ID[chain],
                    label="Etherscan V2",
                    tokenlist=tokenlist,
                ),
            )
        )

    if not specs:
        raise WalletHistoryError(_no_provider_hint(chain))

    errors: list[str] = []
    for name, fn in specs:
        try:
            ledger = fn()
        except Exception as e:  # noqa: BLE001 - 换下一个源前保留原因
            errors.append(f"{name}: {e}")
            continue
        ledger.providers.append(name)
        return ledger
    raise WalletHistoryError(
        f"{chain} 链上历史查询失败: "
        + "; ".join(errors)
        + "\n"
        + _no_provider_hint(chain)
    )


def _no_provider_hint(chain: str) -> str:
    if chain in ("polygon", "arbitrum", "bsc"):
        return (
            f"{chain} 的免费公共浏览器接口目前不可用或需要 key。可在 "
            f"{CONFIG_NAME} 配置 explorer_key (Etherscan V2 免费 key, 或设环境变量 "
            f"{EXPLORER_KEY_ENV}), 或用 explorers/blockscout_urls 指向自建/其它兼容服务。"
        )
    return "请稍后重试, 或检查网络是否能访问公共浏览器接口。"


# ---------------------------------------------------------------------------
# EVM: Routescan Data API
# ---------------------------------------------------------------------------


def _routescan_pages(
    url: str,
    cfg: dict,
    *,
    limit: int = 100,
    stop_ts: int | None = None,
) -> tuple[list[dict], bool]:
    """遍历 Routescan 游标分页; 返回 (items, truncated)."""
    items: list[dict] = []
    truncated = False
    next_url: str | None = url
    for page in range(int(cfg["max_pages"])):
        if not next_url:
            break
        body = _http_json(
            next_url,
            {"limit": limit, "sort": "desc"} if page == 0 else None,
            timeout=float(cfg["timeout"]),
        )
        if not isinstance(body, dict):
            raise WalletHistoryError("Routescan 返回结构异常")
        chunk = [x for x in (body.get("items") or []) if isinstance(x, dict)]
        items.extend(chunk)
        link = body.get("link") or {}
        next_url = link.get("next")
        if next_url and str(next_url).startswith("/"):
            next_url = "https://api.routescan.io" + str(next_url)
        if not next_url:
            break
        if stop_ts is not None and chunk:
            oldest = min(
                (t for t in (_parse_ts(x.get("timestamp")) for x in chunk) if t),
                default=None,
            )
            if oldest is not None and oldest < stop_ts:
                break
        if page == int(cfg["max_pages"]) - 1:
            truncated = True
    return items, truncated


def _fetch_evm_routescan(
    chain_id: int,
    chain: str,
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    tokenlist: str | None = None,
) -> Ledger:
    base = (
        f"https://api.routescan.io/v2/network/mainnet/evm/{chain_id}/address/{address}"
    )
    ledger = Ledger(chain=chain, address=address)
    native = _native_asset(chain)
    ledger.trust(native)
    known = _known_contracts(chain, tokenlist)

    # 1. 当前原生币余额
    body = _http_json(f"{base}/gas-balance", timeout=float(cfg["timeout"]))
    if isinstance(body, dict):
        items = body.get("items") or []
        if items and isinstance(items[0], dict):
            raw = _to_float(items[0].get("balance"))
            if raw is not None:
                ledger.set_current(native, raw / (10**native.decimals))

    # 2. 当前 ERC-20 持仓 (包含 tokenlist 之外的代币)
    holdings, truncated = _routescan_pages(f"{base}/erc20-holdings", cfg)
    ledger.truncated = ledger.truncated or truncated
    for item in holdings:
        contract = item.get("tokenAddress")
        if not contract:
            continue
        asset = _token_asset(
            chain,
            contract,
            item.get("tokenSymbol"),
            item.get("tokenDecimals"),
        )
        amount = _raw_to_amount(item.get("tokenQuantity"), asset.decimals)
        if amount is not None and amount > 0:
            value_usd = _to_float(item.get("tokenValueInUsd"))
            if value_usd is None:
                price = _to_float(item.get("tokenPrice"))
                value_usd = amount * price if price is not None else None
            ledger.set_current(asset, amount, value_usd)
            if _token_key(chain, str(contract)) in known or (
                value_usd is not None and value_usd >= 1.0
            ):
                ledger.trust(asset)

    # 3. 窗口内 ERC-20 转账
    rows, truncated = _routescan_pages(f"{base}/erc20-transfers", cfg, stop_ts=start_ts)
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        contract = row.get("tokenAddress")
        if not contract:
            continue
        asset = _token_asset(
            chain,
            contract,
            row.get("tokenSymbol"),
            row.get("tokenDecimals"),
        )
        amount = _raw_to_amount(row.get("amount"), asset.decimals)
        if amount is None or amount == 0:
            continue
        src = str(row.get("from") or "").lower()
        dst = str(row.get("to") or "").lower()
        addr = address.lower()
        if src == addr:
            ledger.add_transfer(ts, asset, -amount, str(row.get("txHash") or ""))
        if dst == addr:
            ledger.add_transfer(ts, asset, amount, str(row.get("txHash") or ""))

    # 4. 窗口内普通交易 (原生币转入/转出 + 手续费)
    rows, truncated = _routescan_pages(f"{base}/transactions", cfg, stop_ts=start_ts)
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        src = str(row.get("from") or "").lower()
        dst = str(row.get("to") or "").lower()
        addr = address.lower()
        value = _to_float(row.get("value")) or 0.0
        gas_used = _to_float(row.get("gasUsed")) or 0.0
        gas_price = _to_float(row.get("gasPrice")) or 0.0
        fee = gas_used * gas_price
        if fee <= 0:
            fee = _to_float(row.get("burnedFees")) or 0.0
        ok = bool(row.get("status", True))
        if src == addr:
            spend = (value if ok else 0.0) + fee
            if spend:
                ledger.add_transfer(
                    ts,
                    native,
                    -spend / (10**native.decimals),
                    str(row.get("id") or ""),
                )
        elif dst == addr and ok and value:
            ledger.add_transfer(
                ts,
                native,
                value / (10**native.decimals),
                str(row.get("id") or ""),
            )

    # 5. 内部交易: 合约转给钱包 / 钱包经合约转出的原生币
    try:
        rows, truncated = _routescan_pages(
            f"{base}/internal-operations", cfg, stop_ts=start_ts
        )
        ledger.truncated = ledger.truncated or truncated
        for row in rows:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            if not bool(row.get("status", True)):
                continue
            value = _to_float(row.get("value")) or 0.0
            if not value:
                continue
            src = str(row.get("from") or "").lower()
            dst = str(row.get("to") or "").lower()
            addr = address.lower()
            txid = str(row.get("txHash") or "")
            if dst == addr:
                ledger.add_transfer(ts, native, value / (10**native.decimals), txid)
            elif src == addr:
                ledger.add_transfer(ts, native, -value / (10**native.decimals), txid)
    except WalletHistoryError as e:
        ledger.notes.append(f"内部交易未取到, 原生币历史可能偏低: {e}")

    return ledger


# ---------------------------------------------------------------------------
# EVM: Blockscout v2
# ---------------------------------------------------------------------------


def _blockscout_pages(
    url: str,
    cfg: dict,
    *,
    stop_ts: int | None = None,
) -> tuple[list[dict], bool]:
    """遍历 Blockscout v2 的 next_page_params 分页."""
    items: list[dict] = []
    params: dict | None = None
    truncated = False
    for page in range(int(cfg["max_pages"])):
        body = _http_json(url, params, timeout=float(cfg["timeout"]))
        if isinstance(body, list):  # 少数接口直接返回数组
            return [x for x in body if isinstance(x, dict)], False
        if not isinstance(body, dict):
            raise WalletHistoryError("Blockscout 返回结构异常")
        chunk = [x for x in (body.get("items") or []) if isinstance(x, dict)]
        items.extend(chunk)
        params = body.get("next_page_params")
        if not params:
            break
        if stop_ts is not None and chunk:
            oldest = min(
                (t for t in (_parse_ts(x.get("timestamp")) for x in chunk) if t),
                default=None,
            )
            if oldest is not None and oldest < stop_ts:
                break
        if page == int(cfg["max_pages"]) - 1:
            truncated = True
    return items, truncated


def _bs_hash(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or "")
    return str(value or "")


def _fetch_evm_blockscout(
    base_url: str,
    chain: str,
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    tokenlist: str | None = None,
) -> Ledger:
    base = base_url.rstrip("/") + f"/api/v2/addresses/{address}"
    ledger = Ledger(chain=chain, address=address)
    native = _native_asset(chain)
    ledger.trust(native)
    known = _known_contracts(chain, tokenlist)

    # 当前原生币余额
    info = _http_json(base, timeout=float(cfg["timeout"]))
    if isinstance(info, dict):
        amount = _raw_to_amount(info.get("coin_balance"), native.decimals)
        if amount is not None:
            ledger.set_current(native, amount)

    # 当前 ERC-20 持仓; Blockscout 的 token-balances 偶尔很慢/超时:
    # 失败时退回 wallet 的 tokenlist 当前余额, 不阻断转账回放。
    try:
        balances = _http_json(f"{base}/token-balances", timeout=float(cfg["timeout"]))
        if isinstance(balances, list):
            for item in balances:
                if not isinstance(item, dict):
                    continue
                token = item.get("token") or {}
                contract = token.get("address_hash") or token.get("address")
                if not contract:
                    continue
                if str(token.get("type") or "").upper() not in ("", "ERC-20"):
                    continue
                asset = _token_asset(
                    chain,
                    contract,
                    token.get("symbol"),
                    token.get("decimals"),
                )
                amount = _raw_to_amount(item.get("value"), asset.decimals)
                if amount is not None and amount > 0:
                    rate = _to_float(token.get("exchange_rate"))
                    value_usd = amount * rate if rate is not None else None
                    ledger.set_current(asset, amount, value_usd)
                    if _token_key(chain, str(contract)) in known or (
                        value_usd is not None and value_usd >= 1.0
                    ):
                        ledger.trust(asset)
    except WalletHistoryError as e:
        ledger.notes.append(f"Blockscout 当前代币持仓未取到, 改用 tokenlist 兜底: {e}")
        try:
            from . import wallet

            raw = wallet.import_wallet(chain, address)
            for h in raw.get("holdings", []):
                contract = h.get("contract")
                if not contract:
                    continue
                asset = _token_asset(
                    chain,
                    contract,
                    str(h.get("symbol") or "").split("-")[0],
                    h.get("decimals"),
                )
                ledger.set_current(asset, float(h.get("quantity") or 0))
                ledger.trust(asset)
        except Exception as e2:  # noqa: BLE001 - 兜底失败只记提示
            ledger.notes.append(f"当前代币持仓兜底也失败: {e2}")

    # 窗口内 ERC-20 转账
    rows, truncated = _blockscout_pages(
        f"{base}/token-transfers?type=ERC-20", cfg, stop_ts=start_ts
    )
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        token = row.get("token") or {}
        contract = token.get("address_hash") or token.get("address")
        if not contract:
            continue
        total = row.get("total") or {}
        asset = _token_asset(
            chain,
            contract,
            token.get("symbol"),
            token.get("decimals") or total.get("decimals"),
        )
        amount = _raw_to_amount(total.get("value"), asset.decimals)
        if amount is None or amount == 0:
            continue
        src = _bs_hash(row.get("from")).lower()
        dst = _bs_hash(row.get("to")).lower()
        addr = address.lower()
        txid = str(row.get("transaction_hash") or "")
        if src == addr:
            ledger.add_transfer(ts, asset, -amount, txid)
        if dst == addr:
            ledger.add_transfer(ts, asset, amount, txid)

    # 窗口内普通交易 (原生币 + 手续费)
    rows, truncated = _blockscout_pages(f"{base}/transactions", cfg, stop_ts=start_ts)
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        src = _bs_hash(row.get("from")).lower()
        dst = _bs_hash(row.get("to")).lower()
        addr = address.lower()
        value = _to_float(row.get("value")) or 0.0
        fee = _to_float(row.get("fee")) or 0.0
        ok = str(row.get("status") or "ok").lower() in ("ok", "success", "true", "1")
        txid = str(row.get("hash") or "")
        if src == addr:
            spend = (value if ok else 0.0) + fee
            if spend:
                ledger.add_transfer(ts, native, -spend / (10**native.decimals), txid)
        elif dst == addr and ok and value:
            ledger.add_transfer(ts, native, value / (10**native.decimals), txid)

    # 内部交易
    try:
        rows, truncated = _blockscout_pages(
            f"{base}/internal-transactions", cfg, stop_ts=start_ts
        )
        ledger.truncated = ledger.truncated or truncated
        for row in rows:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            if not bool(row.get("success", True)):
                continue
            value = _to_float(row.get("value")) or 0.0
            if not value:
                continue
            src = _bs_hash(row.get("from")).lower()
            dst = _bs_hash(row.get("to")).lower()
            addr = address.lower()
            txid = str(row.get("transaction_hash") or row.get("hash") or "")
            if dst == addr:
                ledger.add_transfer(ts, native, value / (10**native.decimals), txid)
            elif src == addr:
                ledger.add_transfer(ts, native, -value / (10**native.decimals), txid)
    except WalletHistoryError as e:
        ledger.notes.append(f"Blockscout 内部交易未取到, 原生币历史可能偏低: {e}")

    return ledger


# ---------------------------------------------------------------------------
# EVM: Etherscan 兼容接口 (Snowtrace / Etherscan V2 / 自定义)
# ---------------------------------------------------------------------------


def _etherscan_result(
    base_url: str,
    params: dict,
    cfg: dict,
    *,
    label: str,
    chain_id: int | None = None,
) -> Any:
    query = dict(params)
    if chain_id is not None:
        query["chainid"] = chain_id
    body = _http_json(base_url, query, timeout=float(cfg["timeout"]))
    if not isinstance(body, dict):
        raise WalletHistoryError(f"{label} 返回结构异常")
    status = str(body.get("status") or "")
    result = body.get("result")
    message = str(body.get("message") or "")
    if status == "1":
        return result
    # Etherscan 的「无记录」也是 status=0, 不能当错误
    if isinstance(result, list):
        return result
    if "no transactions found" in message.lower():
        return []
    if "deprecated" in message.lower() or "deprecated" in str(result).lower():
        raise WalletHistoryError(f"{label} 接口已废弃, 请换用 Etherscan V2 或自定义源")
    if "api key" in str(result).lower() or "apikey" in str(result).lower():
        raise WalletHistoryError(f"{label} 需要 API Key: {_scrub(result)}")
    raise WalletHistoryError(f"{label} 查询失败: {_scrub(message or result or status)}")


def _etherscan_pages(
    base_url: str,
    action: str,
    address: str,
    cfg: dict,
    *,
    label: str,
    chain_id: int | None = None,
    stop_ts: int | None = None,
    offset: int = 1000,
) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    truncated = False
    for page in range(1, int(cfg["max_pages"]) + 1):
        result = _etherscan_result(
            base_url,
            {
                "module": "account",
                "action": action,
                "address": address,
                "page": page,
                "offset": offset,
                "sort": "desc",
            },
            cfg,
            label=label,
            chain_id=chain_id,
        )
        chunk = [x for x in (result or []) if isinstance(x, dict)]
        rows.extend(chunk)
        if len(chunk) < offset:
            break
        if stop_ts is not None and chunk:
            oldest = min(
                (t for t in (_parse_ts(x.get("timeStamp")) for x in chunk) if t),
                default=None,
            )
            if oldest is not None and oldest < stop_ts:
                break
        if page == int(cfg["max_pages"]):
            truncated = True
    return rows, truncated


def _fetch_evm_etherscan_like(
    base_url: str,
    chain: str,
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    *,
    api_key: str | None = None,
    chain_id: int | None = None,
    label: str = "Etherscan",
    tokenlist: str | None = None,
) -> Ledger:
    key_params = {"apikey": api_key} if api_key else {}
    ledger = Ledger(chain=chain, address=address)
    native = _native_asset(chain)
    ledger.trust(native)
    known = _known_contracts(chain, tokenlist)

    # 当前原生币余额
    result = _etherscan_result(
        base_url,
        {"module": "account", "action": "balance", "address": address, **key_params},
        cfg,
        label=label,
        chain_id=chain_id,
    )
    amount = _raw_to_amount(result, native.decimals)
    if amount is not None:
        ledger.set_current(native, amount)

    # 当前代币持仓: addresstokenbalance 一次拿全; 不支持时按转账合约逐个查
    token_contracts: set[str] = set()
    try:
        rows, _ = _etherscan_pages(
            base_url,
            "addresstokenbalance",
            address,
            cfg,
            label=label,
            chain_id=chain_id,
            offset=100,
        )
        for row in rows:
            contract = row.get("TokenAddress") or row.get("tokenAddress")
            if not contract:
                continue
            token_contracts.add(str(contract))
            asset = _token_asset(
                chain,
                contract,
                row.get("TokenSymbol") or row.get("tokenSymbol"),
                row.get("TokenDivisor") or row.get("tokenDecimal"),
            )
            amount = _raw_to_amount(
                row.get("TokenQuantity") or row.get("tokenQuantity"),
                asset.decimals,
            )
            if amount is not None and amount > 0:
                price = _to_float(row.get("TokenPriceUSD") or row.get("tokenPriceUSD"))
                value_usd = amount * price if price is not None else None
                ledger.set_current(asset, amount, value_usd)
                if _token_key(chain, str(contract)) in known or (
                    value_usd is not None and value_usd >= 1.0
                ):
                    ledger.trust(asset)
    except WalletHistoryError:
        # 老接口/部分浏览器没有 addresstokenbalance: 先留空, 等转账合约收集完再查
        rows = []

    # 窗口内 ERC-20 转账
    rows, truncated = _etherscan_pages(
        base_url,
        "tokentx",
        address,
        cfg,
        label=label,
        chain_id=chain_id,
        stop_ts=start_ts,
    )
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timeStamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        contract = row.get("contractAddress")
        if not contract:
            continue
        token_contracts.add(str(contract))
        asset = _token_asset(
            chain,
            contract,
            row.get("tokenSymbol"),
            row.get("tokenDecimal"),
        )
        amount = _raw_to_amount(row.get("value"), asset.decimals)
        if amount is None or amount == 0:
            continue
        src = str(row.get("from") or "").lower()
        dst = str(row.get("to") or "").lower()
        addr = address.lower()
        txid = str(row.get("hash") or "")
        if src == addr:
            ledger.add_transfer(ts, asset, -amount, txid)
        if dst == addr:
            ledger.add_transfer(ts, asset, amount, txid)

    # addresstokenbalance 不可用时: 对窗口内出现过的代币补查当前余额
    known_current = set(ledger.current)
    for contract in token_contracts:
        key = _token_key(chain, contract)
        if key in known_current:
            continue
        asset = ledger.assets.get(key) or _token_asset(chain, contract, None, 0)
        try:
            result = _etherscan_result(
                base_url,
                {
                    "module": "account",
                    "action": "tokenbalance",
                    "contractaddress": contract,
                    "address": address,
                    **key_params,
                },
                cfg,
                label=label,
                chain_id=chain_id,
            )
            amount = _raw_to_amount(result, asset.decimals)
        except WalletHistoryError as e:
            ledger.notes.append(f"{asset.symbol} 当前余额未取到: {e}")
            continue
        if amount is not None and amount > 0:
            ledger.set_current(asset, amount)
            if _token_key(chain, str(contract)) in known:
                ledger.trust(asset)

    # 窗口内普通交易 (原生币 + 手续费)
    rows, truncated = _etherscan_pages(
        base_url,
        "txlist",
        address,
        cfg,
        label=label,
        chain_id=chain_id,
        stop_ts=start_ts,
    )
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("timeStamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        src = str(row.get("from") or "").lower()
        dst = str(row.get("to") or "").lower()
        addr = address.lower()
        value = _to_float(row.get("value")) or 0.0
        gas_used = _to_float(row.get("gasUsed")) or 0.0
        gas_price = _to_float(row.get("gasPrice")) or 0.0
        fee = gas_used * gas_price
        ok = str(row.get("isError") or "0") != "1"
        txid = str(row.get("hash") or "")
        if src == addr:
            spend = (value if ok else 0.0) + fee
            if spend:
                ledger.add_transfer(ts, native, -spend / (10**native.decimals), txid)
        elif dst == addr and ok and value:
            ledger.add_transfer(ts, native, value / (10**native.decimals), txid)

    # 内部交易
    try:
        rows, truncated = _etherscan_pages(
            base_url,
            "txlistinternal",
            address,
            cfg,
            label=label,
            chain_id=chain_id,
            stop_ts=start_ts,
        )
        ledger.truncated = ledger.truncated or truncated
        for row in rows:
            ts = _parse_ts(row.get("timeStamp"))
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            if str(row.get("isError") or "0") == "1":
                continue
            value = _to_float(row.get("value")) or 0.0
            if not value:
                continue
            src = str(row.get("from") or "").lower()
            dst = str(row.get("to") or "").lower()
            addr = address.lower()
            txid = str(row.get("hash") or "")
            if dst == addr:
                ledger.add_transfer(ts, native, value / (10**native.decimals), txid)
            elif src == addr:
                ledger.add_transfer(ts, native, -value / (10**native.decimals), txid)
    except WalletHistoryError as e:
        ledger.notes.append(f"{label} 内部交易未取到, 原生币历史可能偏低: {e}")

    return ledger


# ---------------------------------------------------------------------------
# TRON
# ---------------------------------------------------------------------------


def _tron_get(path: str, cfg: dict) -> dict:
    from . import wallet

    try:
        body = wallet._tron_get(
            wallet._CHAIN_CONFIG["tron"]["rpc"], path, float(cfg["timeout"])
        )
    except RuntimeError as e:
        raise WalletHistoryError(f"TronGrid 请求失败: {e}") from e
    if not isinstance(body, dict):
        raise WalletHistoryError("TronGrid 返回结构异常")
    return body


def _tron_pages(path_base: str, cfg: dict, start_ts: int) -> tuple[list[dict], bool]:
    """遍历 TronGrid 的 fingerprint 分页."""
    rows: list[dict] = []
    fingerprint: str | None = None
    truncated = False
    for page in range(int(cfg["max_pages"])):
        path = (
            f"{path_base}?only_confirmed=true&limit=200&min_timestamp={start_ts * 1000}"
        )
        if fingerprint:
            path += f"&fingerprint={fingerprint}"
        body = _tron_get(path, cfg)
        chunk = [x for x in (body.get("data") or []) if isinstance(x, dict)]
        rows.extend(chunk)
        meta = body.get("meta") or {}
        fingerprint = meta.get("fingerprint")
        if not fingerprint or len(chunk) < 200:
            break
        if page == int(cfg["max_pages"]) - 1:
            truncated = True
    return rows, truncated


def _tron_fee(tx: dict) -> int:
    """单笔 TRON 交易手续费 (sun), 优先取 ret[0].fee."""
    ret = tx.get("ret") or []
    if isinstance(ret, list) and ret and isinstance(ret[0], dict):
        fee = _to_float(ret[0].get("fee"))
        if fee:
            return int(fee)
    total = 0
    for key in ("fee", "energy_fee", "net_fee"):
        v = _to_float(tx.get(key))
        if v:
            total += int(v)
    return total


def _tron_hex_address(address: str) -> str:
    from . import wallet

    payload = wallet._b58check_decode_tron(address)
    return payload.hex() if payload else ""


def _fetch_tron(
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    tokenlist: str | None,
) -> Ledger:
    from . import wallet

    ledger = Ledger(chain="tron", address=address)
    native = _native_asset("tron")
    ledger.trust(native)
    addr_hex = _tron_hex_address(address)
    tokens = wallet._load_tokenlist(tokenlist).get("tron", {})

    # 1. TRC-20 转账 (先处理, 用返回的 token_info 补全 symbol/decimals)
    rows, truncated = _tron_pages(
        f"/v1/accounts/{address}/transactions/trc20", cfg, start_ts
    )
    ledger.truncated = ledger.truncated or truncated
    for row in rows:
        ts = _parse_ts(row.get("block_timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        info = row.get("token_info") or {}
        contract = info.get("address")
        if not contract:
            continue
        asset = _token_asset("tron", contract, info.get("symbol"), info.get("decimals"))
        amount = _raw_to_amount(row.get("value"), asset.decimals)
        if amount is None or amount == 0:
            continue
        src = str(row.get("from") or "")
        dst = str(row.get("to") or "")
        txid = str(row.get("transaction_id") or "")
        if src == address:
            ledger.add_transfer(ts, asset, -amount, txid)
        if dst == address:
            ledger.add_transfer(ts, asset, amount, txid)

    # 2. 普通交易 (TRX 转账 + 合约手续费)
    rows, truncated = _tron_pages(f"/v1/accounts/{address}/transactions", cfg, start_ts)
    ledger.truncated = ledger.truncated or truncated
    staking_seen = False
    for tx in rows:
        ts = _parse_ts(tx.get("block_timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        raw_data = tx.get("raw_data") or {}
        contracts = raw_data.get("contract") or []
        if not contracts or not isinstance(contracts[0], dict):
            continue
        contract = contracts[0]
        ctype = str(contract.get("type") or "")
        value = (contract.get("parameter") or {}).get("value") or {}
        owner = str(value.get("owner_address") or "")
        fee = _tron_fee(tx)
        txid = str(tx.get("txID") or "")
        if ctype == "TransferContract":
            amount = _raw_to_amount(value.get("amount"), native.decimals) or 0.0
            to_addr = str(value.get("to_address") or "")
            if owner == addr_hex:
                ledger.add_transfer(
                    ts, native, -(amount + fee / 10**native.decimals), txid
                )
            elif to_addr == addr_hex and amount:
                ledger.add_transfer(ts, native, amount, txid)
        else:
            # 合约调用/质押/投票等: 只扣手续费; 代币数量已由 trc20 列表覆盖
            if owner == addr_hex and fee:
                ledger.add_transfer(ts, native, -fee / 10**native.decimals, txid)
            if any(
                k in ctype
                for k in ("Freeze", "Unfreeze", "Delegate", "Undelegate", "Withdraw")
            ):
                staking_seen = True

    # 3. 当前余额: 可用 TRX + 全部 TRC-20
    account = _tron_get(f"/v1/accounts/{address}", cfg)
    data = account.get("data") or []
    acct = data[0] if data and isinstance(data[0], dict) else {}
    spendable = _raw_to_amount(acct.get("balance"), native.decimals)
    if spendable is not None:
        ledger.set_current(native, spendable)

    balances: dict[str, int] = {}
    for item in acct.get("trc20") or []:
        if not isinstance(item, dict):
            continue
        for contract, raw in item.items():
            try:
                balances[contract] = balances.get(contract, 0) + int(raw)
            except (TypeError, ValueError):
                continue
    for contract, raw in balances.items():
        info = ledger.assets.get(_token_key("tron", contract)) or tokens.get(contract)
        if isinstance(info, Asset):
            asset = info
        elif isinstance(info, dict):
            asset = _token_asset(
                "tron", contract, info.get("symbol"), info.get("decimals")
            )
        else:
            asset = _token_asset("tron", contract, None, 0)
        amount = _raw_to_amount(raw, asset.decimals)
        if amount is not None and amount > 0:
            ledger.set_current(asset, amount)
            if contract in tokens or asset.key in ledger.trusted:
                ledger.trust(asset)

    if staking_seen:
        ledger.notes.append(
            "检测到质押/解冻类交易: 历史曲线只计入可用 TRX, 不含质押/解冻中的 TRX"
        )
    ledger.providers.append("TronGrid")
    return ledger


# ---------------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------------


def _sol_rpc(method: str, params: list, cfg: dict) -> Any:
    from . import wallet

    hosts = wallet._solana_rpc_hosts(wallet._CHAIN_CONFIG["solana"])
    try:
        return wallet._try_rpc_hosts(
            hosts,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            float(cfg["timeout"]),
        )
    except RuntimeError as e:
        raise WalletHistoryError(f"Solana RPC 请求失败: {e}") from e


def _sol_key(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("pubkey") or "")
    return str(item or "")


def _sol_token_deltas(meta: dict, address: str) -> dict[str, tuple[float, int]]:
    """单笔交易的 SPL 余额变化: mint → (数量变化, decimals)."""
    out: dict[str, tuple[float, int]] = {}
    for key in ("preTokenBalances", "postTokenBalances"):
        for item in meta.get(key) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("owner") or "") != address:
                continue
            mint = str(item.get("mint") or "")
            if not mint:
                continue
            amount_info = item.get("uiTokenAmount") or {}
            try:
                raw = int(str(amount_info.get("amount") or 0))
                dec = max(0, int(amount_info.get("decimals") or 0))
            except (TypeError, ValueError):
                continue
            sign = 1 if key == "postTokenBalances" else -1
            prev, _ = out.get(mint, (0.0, dec))
            out[mint] = (prev + sign * raw / (10**dec), dec)
    return out


def _fetch_solana(
    address: str,
    start_ts: int,
    end_ts: int,
    cfg: dict,
    tokenlist: str | None,
) -> Ledger:
    from concurrent.futures import ThreadPoolExecutor

    from . import wallet

    ledger = Ledger(chain="solana", address=address)
    native = _native_asset("solana")
    ledger.trust(native)
    tokens = wallet._load_tokenlist(tokenlist).get("solana", {})

    def _asset_for_mint(mint: str, decimals: int) -> Asset:
        info = tokens.get(mint)
        if isinstance(info, dict):
            asset = _token_asset(
                "solana", mint, info.get("symbol"), info.get("decimals", decimals)
            )
            ledger.trust(asset)
            return asset
        return _token_asset("solana", mint, None, decimals, fallback=_short(mint, 8))

    # 1. 当前余额: SOL + 全部 SPL (不限 tokenlist, 否则当前持仓锚点会漏)
    bal = _sol_rpc("getBalance", [address], cfg)
    if isinstance(bal, dict):
        lamports = _to_float(bal.get("value"))
        if lamports is not None:
            ledger.set_current(native, lamports / (10**native.decimals))

    current_mints: dict[str, tuple[int, int]] = {}
    for program in (wallet._SOL_TOKEN_PROGRAM, wallet._SOL_TOKEN2022_PROGRAM):
        try:
            res = _sol_rpc(
                "getTokenAccountsByOwner",
                [address, {"programId": program}, {"encoding": "jsonParsed"}],
                cfg,
            )
        except WalletHistoryError as e:
            ledger.notes.append(f"SPL 当前余额部分未取到: {e}")
            continue
        if not isinstance(res, dict):
            continue
        for item in res.get("value") or []:
            try:
                info = item["account"]["data"]["parsed"]["info"]
                mint = str(info.get("mint") or "")
                amount_info = info.get("tokenAmount") or {}
                raw = int(str(amount_info.get("amount") or 0))
                dec = max(0, int(amount_info.get("decimals") or 0))
            except (KeyError, TypeError, ValueError):
                continue
            if not mint or raw <= 0:
                continue
            prev_raw, _ = current_mints.get(mint, (0, dec))
            current_mints[mint] = (prev_raw + raw, dec)
    for mint, (raw, dec) in current_mints.items():
        ledger.set_current(_asset_for_mint(mint, dec), raw / (10**dec))

    # 2. 签名列表 (从新到旧, 截到窗口起点)
    signatures: list[dict] = []
    before: str | None = None
    truncated = False
    for page in range(int(cfg["max_pages"])):
        params: dict[str, Any] = {"limit": 1000}
        if before:
            params["before"] = before
        try:
            res = _sol_rpc("getSignaturesForAddress", [address, params], cfg)
        except WalletHistoryError as e:
            ledger.notes.append(f"签名列表未取到: {e}")
            break
        chunk = [x for x in (res or []) if isinstance(x, dict)]
        if not chunk:
            break
        signatures.extend(chunk)
        before = str(chunk[-1].get("signature") or "") or None
        oldest = min(
            (t for t in (_parse_ts(x.get("blockTime")) for x in chunk) if t),
            default=None,
        )
        if oldest is not None and oldest < start_ts:
            break
        if len(signatures) >= int(cfg["max_transactions"]):
            truncated = True
            break
        if page == int(cfg["max_pages"]) - 1:
            truncated = True

    # 只保留窗口内、且未超过上限的签名
    selected: list[dict] = []
    for sig in signatures:
        ts = _parse_ts(sig.get("blockTime"))
        if ts is None:
            continue
        if ts < start_ts:
            continue
        if ts > end_ts:
            continue
        selected.append(sig)
        if len(selected) >= int(cfg["max_transactions"]):
            truncated = True
            break
    ledger.truncated = ledger.truncated or truncated

    def _fetch_one(sig: str) -> dict | None:
        try:
            res = _sol_rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                cfg,
            )
        except WalletHistoryError:
            return None
        return res if isinstance(res, dict) else None

    # getTransaction 是逐笔请求: 小并发 (4) 控制公共节点压力
    with ThreadPoolExecutor(max_workers=4) as pool:
        txs = list(pool.map(_fetch_one, [str(s.get("signature")) for s in selected]))

    failed = 0
    for sig, tx in zip(selected, txs):
        if not tx:
            failed += 1
            continue
        ts = _parse_ts(tx.get("blockTime")) or _parse_ts(sig.get("blockTime"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue
        txid = str(sig.get("signature") or "")
        meta = tx.get("meta") or {}
        message = (tx.get("transaction") or {}).get("message") or {}
        keys = [_sol_key(k) for k in (message.get("accountKeys") or [])]
        try:
            idx = keys.index(address)
        except ValueError:
            idx = -1
        if idx >= 0:
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            if idx < len(pre) and idx < len(post):
                delta = (float(post[idx]) - float(pre[idx])) / (10**native.decimals)
                ledger.add_transfer(ts, native, delta, txid)
        for mint, (delta, dec) in _sol_token_deltas(meta, address).items():
            if abs(delta) > 0:
                ledger.add_transfer(ts, _asset_for_mint(mint, dec), delta, txid)

    if failed:
        ledger.notes.append(
            f"{failed} 笔 Solana 交易详情未取到 (公共节点可能已裁剪历史), 余额可能偏差"
        )
    if ledger.truncated:
        ledger.notes.append(
            f"历史记录超过上限 (最多 {cfg['max_transactions']} 笔), 只回放了最近部分"
        )
    ledger.providers.append("Solana RPC")
    return ledger


__all__ = [
    "Asset",
    "Ledger",
    "Transfer",
    "WalletHistoryError",
    "SUPPORTED_CHAINS",
    "config_path",
    "fetch_ledger",
    "load_config",
    "reconstruct_daily_balances",
    "reset",
]
