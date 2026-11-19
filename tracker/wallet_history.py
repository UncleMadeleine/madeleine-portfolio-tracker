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
from typing import Any

from . import config
from .chains import CHAIN_CONFIG as _CHAIN_CONFIG
from .chains import EVM_CHAIN_ID as _EVM_CHAIN_ID
from .paths import TEMPLATES_DIR, data_home
from .util import finite_float as _to_float

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


def _oldest_ts(rows: list[dict]) -> int | None:
    """一批转账记录里最早的时间戳 (用于分页提前停止判断)."""
    return min(
        (t for t in (_parse_ts(x.get("timestamp")) for x in rows) if t),
        default=None,
    )


def _day_start_ts(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _day_end_ts(day: date) -> int:
    """day 当天最后一秒 (含)."""
    return _day_start_ts(day + timedelta(days=1)) - 1


def _utc_day(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


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
    info = _CHAIN_CONFIG[chain]
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
# EVM: 公共入口 (具体浏览器适配器见 wallet_evm)
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
    from . import wallet_evm

    return wallet_evm.fetch_evm(chain, address, start_ts, end_ts, cfg, tokenlist)


# ---------------------------------------------------------------------------
# TRON
# ---------------------------------------------------------------------------


def _tron_get(path: str, cfg: dict) -> dict:
    from . import wallet

    try:
        body = wallet._tron_get(
            _CHAIN_CONFIG["tron"]["rpc"], path, float(cfg["timeout"])
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
        # TronGrid 会返回失败交易: 失败的转账金额并未移动, 只能计手续费。
        ret = tx.get("ret") or []
        success = True
        if isinstance(ret, list) and ret and isinstance(ret[0], dict):
            code = ret[0].get("contractRet")
            if code:
                success = str(code).upper() == "SUCCESS"
        if ctype == "TransferContract":
            amount = (
                _raw_to_amount(value.get("amount"), native.decimals) or 0.0
            ) if success else 0.0
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

    hosts = wallet._solana_rpc_hosts(_CHAIN_CONFIG["solana"])
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
    wsol_raw = 0
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
            # wSOL 与 wallet.import_wallet 同口径并入原生 SOL: 否则当前
            # 锚点漏掉包装 SOL, 而曲线里又按无行情的 "So111..." 丢弃。
            if mint == wallet._SOL_WSOL_MINT:
                wsol_raw += raw
                continue
            prev_raw, _ = current_mints.get(mint, (0, dec))
            current_mints[mint] = (prev_raw + raw, dec)
    for mint, (raw, dec) in current_mints.items():
        ledger.set_current(_asset_for_mint(mint, dec), raw / (10**dec))
    if wsol_raw:
        native_now = float(ledger.current.get(native.key) or 0.0)
        ledger.set_current(
            native, native_now + wsol_raw / (10**native.decimals)
        )
        ledger.notes.append(
            f"wSOL 已并入原生 SOL ({wsol_raw / 10**native.decimals:g})"
        )

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
                # 包装 SOL 的增减与原生 SOL 余额变化互补: 并入同一个资产键,
                # 否则 wrap/unwrap 会在曲线上凭空多出一笔/少掉一笔。
                if mint == wallet._SOL_WSOL_MINT:
                    ledger.add_transfer(ts, native, delta, txid)
                else:
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
