"""EVM 链历史查询适配器: Routescan / Blockscout / Etherscan 兼容接口.

从 wallet_history 拆出: 三种浏览器源的「原生币余额 → 代币持仓 → 代币转账 →
普通交易」流程独立成模块, 公共 Ledger 组装/缓存/脱敏仍由 wallet_history
提供。wallet_history 只在需要时惰性导入本模块, 避免循环依赖。
"""

from __future__ import annotations

from typing import Any, Callable

from .chains import DEFAULT_BLOCKSCOUT as _DEFAULT_BLOCKSCOUT
from .chains import ETHERSCAN_V2 as _ETHERSCAN_V2
from .chains import EVM_CHAIN_ID as _EVM_CHAIN_ID
from .chains import ROUTESCAN_CHAINS as _ROUTESCAN_CHAINS
from .chains import SNOWTRACE as _SNOWTRACE
from .util import finite_float as _to_float
from .wallet_history import (
    CONFIG_NAME,
    EXPLORER_KEY_ENV,
    Ledger,
    WalletHistoryError,
    _http_json,
    _known_contracts,
    _native_asset,
    _oldest_ts,
    _parse_ts,
    _raw_to_amount,
    _scrub,
    _token_asset,
    _token_key,
)

# ---------------------------------------------------------------------------
# EVM: 公共入口 + 源链切换
# ---------------------------------------------------------------------------


def fetch_evm(
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
            oldest = _oldest_ts(chunk)
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
            # Routescan 的 internal-operations 会把每笔交易的根调用
            # (opIndex=0) 也列出来, 它和 /transactions 的同一条记录完全重复。
            # 不跳过会把所有原生币转入/转出计算两遍, 反推历史余额时整体偏移。
            try:
                if row.get("opIndex") is not None and int(row["opIndex"]) == 0:
                    continue
            except (TypeError, ValueError):
                pass
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
            oldest = _oldest_ts(chunk)
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
        # Blockscout v2 的 fee 是对象 ({"type":"actual","value":"..."}),
        # 直接 float() 会得到 0, 导致所有转出交易都不扣手续费; 部分版本/
        # 自建实例仍给数字或 gas_used×gas_price, 三种形态都要兼容。
        raw_fee = row.get("fee")
        if isinstance(raw_fee, dict):
            fee = _to_float(raw_fee.get("value")) or 0.0
        else:
            fee = _to_float(raw_fee) or 0.0
        if fee <= 0:
            fee = (_to_float(row.get("gas_used")) or 0.0) * (
                _to_float(row.get("gas_price")) or 0.0
            )
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


