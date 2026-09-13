"""链上钱包余额查询: 轻钱包策略 (内置 tokenlist + balanceOf RPC 调用).

支持 EVM 链 (ETH / BSC / Polygon / Arbitrum / Avalanche):
  - 主币余额: eth_getBalance
  - ERC-20 余额: balanceOf(address) 批量调用
  - 内置主流代币 tokenlist; 支持 --tokenlist 加载外部 JSON 覆盖

不依赖 web3.py, 直接 HTTP JSON-RPC (requests); 无需 API key.
公钥/地址只读查询, 不涉及私钥操作.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 内置 tokenlist (contract_address → symbol, decimals)
# 覆盖 ETH / BSC / Polygon / Arbitrum / Avalanche 五条 EVM 链的主流 ERC-20
# decimals 按各链实际情况填写 (同一合约在跨链时 decimals 通常相同)
# 地址经链上 eth_getCode + symbol()/decimals() 与 CoinGecko 平台地址双向核对;
# symbol 是写入 portfolio.json 的计价基础 (BASE-QUOTE), 需为 Yahoo 可报价代码
# ---------------------------------------------------------------------------

_BUILTIN_TOKENLIST: dict[str, dict[str, dict[str, Any]]] = {
    "eth": {
        "0xdAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT", "decimals": 6},
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {"symbol": "USDC", "decimals": 6},
        "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI",  "decimals": 18},
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC", "decimals": 8},
        "0x514910771AF9Ca656af840dff83E8264EcF986CA": {"symbol": "LINK", "decimals": 18},
        "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984": {"symbol": "UNI",  "decimals": 18},
        "0x7fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9": {"symbol": "AAVE", "decimals": 18},
        "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE": {"symbol": "SHIB", "decimals": 18},
        "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84": {"symbol": "stETH","decimals": 18},
        "0xaea46A60368A7bD060eec7DF8CBa43b7EF41Ad85": {"symbol": "FET",  "decimals": 18},
        "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32": {"symbol": "LDO",  "decimals": 18},
        "0xBBbbCA6A901c926F240b89EacB641d8Aec7AEafD": {"symbol": "LRC",  "decimals": 18},
        "0xfB7B4564402E5500dB5bB6d63Ae671302777C75a": {"symbol": "DEXT", "decimals": 18},
    },
    "bsc": {
        "0x55d398326f99059fF775485246999027B3197955": {"symbol": "USDT", "decimals": 18},
        "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d": {"symbol": "USDC", "decimals": 18},
        "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3": {"symbol": "DAI",  "decimals": 18},
        "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c": {"symbol": "BTCB", "decimals": 18},
        "0x2170Ed0880ac9A755fd29B2688956BD959F933F8": {"symbol": "ETH",  "decimals": 18},
        "0x2dfF88A56767223A5529eA5960Da7A3F5f766406": {"symbol": "ID",   "decimals": 18},
        "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82": {"symbol": "CAKE", "decimals": 18},
    },
    "polygon": {
        "0xc2132D05D31c914a87C6611C10748AEb04B58e8F": {"symbol": "USDT", "decimals": 6},
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174": {"symbol": "USDC", "decimals": 6},
        "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063": {"symbol": "DAI",  "decimals": 18},
        "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6": {"symbol": "WBTC", "decimals": 8},
        "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619": {"symbol": "ETH",  "decimals": 18},
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270": {"symbol": "WMATIC","decimals": 18},
        "0xb33EaAd8d922B1083446DC23f610c2567fB5180F": {"symbol": "UNI",  "decimals": 18},
    },
    "arbitrum": {
        "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9": {"symbol": "USDT", "decimals": 6},
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": {"symbol": "USDC", "decimals": 6},
        "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1": {"symbol": "DAI",  "decimals": 18},
        "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f": {"symbol": "WBTC", "decimals": 8},
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": {"symbol": "WETH", "decimals": 18},
        "0x5979D7b546E38E414F7E9822514be443A4800529": {"symbol": "wstETH","decimals": 18},
        "0x912CE59144191C1204E64559FE8253a0e49E6548": {"symbol": "ARB",  "decimals": 18},
        "0xB766039cc6DB368759C1E56B79AFfE831d0Cc507": {"symbol": "RPL",  "decimals": 18},
    },
    "avalanche": {
        "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7": {"symbol": "USDT", "decimals": 6},
        "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E": {"symbol": "USDC", "decimals": 6},
        "0xd586E7F844cEa2F87f50152665BCbc2C279D8d70": {"symbol": "DAI",  "decimals": 18},
        "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c": {"symbol": "WBTC", "decimals": 8},
        "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB": {"symbol": "WETH", "decimals": 18},
        "0x6e84a6216eA6dACC71eE8E6b0a5B7322EEbC0fDd": {"symbol": "JOE",  "decimals": 18},
    },
}

# ---------------------------------------------------------------------------
# 链 RPC 配置
# ---------------------------------------------------------------------------

_CHAIN_CONFIG: dict[str, dict[str, Any]] = {
    "eth": {
        "label": "Ethereum",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "rpc": [
            "https://ethereum.publicnode.com",
            "https://eth.drpc.org",
            "https://eth.merkle.io",
            "https://rpc.flashbots.net",
        ],
        "tokenlist": "eth",
    },
    "bsc": {
        "label": "BNB Chain",
        "native_symbol": "BNB",
        "native_decimals": 18,
        "rpc": [
            "https://bsc-dataseed1.binance.org",
            "https://bsc-dataseed2.binance.org",
            "https://bsc.publicnode.com",
        ],
        "tokenlist": "bsc",
    },
    "polygon": {
        "label": "Polygon",
        "native_symbol": "MATIC",
        "native_decimals": 18,
        "rpc": [
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon.drpc.org",
        ],
        "tokenlist": "polygon",
    },
    "arbitrum": {
        "label": "Arbitrum One",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "rpc": [
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.publicnode.com",
            "https://arbitrum.drpc.org",
        ],
        "tokenlist": "arbitrum",
    },
    "avalanche": {
        "label": "Avalanche C-Chain",
        "native_symbol": "AVAX",
        "native_decimals": 18,
        "rpc": [
            "https://api.avax.network/ext/bc/C/rpc",
            "https://avalanche-c-chain-rpc.publicnode.com",
            "https://avalanche.drpc.org",
        ],
        "tokenlist": "avalanche",
    },
}

_SUPPORTED_CHAINS = set(_CHAIN_CONFIG)

# ERC-20 balanceOf 签名
_ERC20_BALANCE_SIG = bytes.fromhex("70a08231")
_ERC20_BALANCE_TOPIC = "0x" + _ERC20_BALANCE_SIG.hex()

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _requests():
    import requests
    return requests


def _rpc_post(url: str, payload: dict, timeout: float = 10.0) -> Any:
    """发送单个 JSON-RPC 请求, 失败时抛 RuntimeError."""
    req = _requests()
    last_err: Exception | None = None
    try:
        r = req.post(url, json=payload, timeout=timeout, headers={"Content-Type": "application/json"})
        if r.status_code == 200:
            body = r.json()
            if "result" in body:
                return body["result"]
            err = body.get("error", {})
            raise RuntimeError(f"RPC 错误 [{err.get('code')}]: {err.get('message', body)}")
        last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        last_err = e
    raise RuntimeError(f"RPC 不可达 {url} ({last_err})")


def _try_rpc_hosts(hosts: list[str], payload: dict, timeout: float = 10.0) -> Any:
    """依次尝试多个 RPC 节点, 任一成功即返回."""
    last_err: Exception | None = None
    for url in hosts:
        try:
            return _rpc_post(url, payload, timeout)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"全部 RPC 节点不可达 ({last_err})")


# ---------------------------------------------------------------------------
# 地址校验
# ---------------------------------------------------------------------------

def _validate_address(address: str, chain: str) -> str:
    """校验地址格式, 返回规范化后的地址."""
    addr = address.strip()
    if not addr:
        raise ValueError("地址不能为空")
    chain_cfg = _CHAIN_CONFIG.get(chain.lower())
    if chain_cfg is None:
        raise ValueError(
            f"不支持的链 '{chain}', 支持: {', '.join(sorted(_SUPPORTED_CHAINS))}"
        )
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise ValueError(
            f"地址格式无效 (需 0x + 40 位十六进制字符): {addr[:10]}..."
        )
    return addr.lower()


# ---------------------------------------------------------------------------
# 余额查询
# ---------------------------------------------------------------------------

def _native_balance(
    address: str, chain: str, chain_cfg: dict[str, Any], timeout: float
) -> dict[str, Any] | None:
    """查询链上原生代币余额 (ETH/BNB/MATIC/AVAX)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }
    raw_hex = _try_rpc_hosts(chain_cfg["rpc"], payload, timeout)
    if raw_hex in (None, "0x0", "0x00"):
        return None
    try:
        raw_wei = int(raw_hex, 16)
    except (ValueError, TypeError):
        return None
    decimals = chain_cfg["native_decimals"]
    amount = raw_wei / (10 ** decimals)
    return {
        "symbol": chain_cfg["native_symbol"],
        "contract": None,
        "amount": amount,
        "decimals": decimals,
        "chain": chain,
        "source": "native",
    }


def _erc20_balance(
    address: str, contract: str, chain: str, decimals: int, timeout: float
) -> dict[str, Any] | None:
    """调用单合约 balanceOf(address), 返回余额 dict 或 None (零余额/失败)."""
    data_topic = _ERC20_BALANCE_TOPIC + address[2:].lower().zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": contract, "data": data_topic}, "latest"],
    }
    raw_hex = _try_rpc_hosts(_CHAIN_CONFIG[chain]["rpc"], payload, timeout)
    # 节点可能返回空串 / 非 hex 载荷 (畸形响应): 一律按无余额处理,
    # 不能把 ValueError 抛给调用方 (import_wallet 只兜 RuntimeError)
    try:
        raw = int(raw_hex, 16)
    except (TypeError, ValueError):
        return None
    if raw == 0:
        return None
    amount = raw / (10 ** decimals)
    return {
        "symbol": None,   # 由调用方从 tokenlist 填入
        "contract": contract,
        "amount": amount,
        "decimals": decimals,
        "chain": chain,
        "source": "erc20",
    }


# ---------------------------------------------------------------------------
# Tokenlist 加载
# ---------------------------------------------------------------------------

def _load_tokenlist(tokenlist_path: str | None) -> dict[str, dict[str, dict[str, Any]]]:
    """加载 tokenlist: 优先外部文件, 否则内置列表."""
    if tokenlist_path:
        p = Path(tokenlist_path)
        if not p.exists():
            raise FileNotFoundError(f"tokenlist 文件不存在: {tokenlist_path}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        loaded: dict[str, dict[str, dict[str, Any]]] = {}
        for chain_key, tokens in raw.items():
            loaded[chain_key] = {}
            for contract, info in tokens.items():
                loaded[chain_key][contract.lower()] = {
                    "symbol": info["symbol"],
                    "decimals": info["decimals"],
                }
        return loaded
    # 内置列表: 把混合-case 的合约地址统一归一化为小写, 方便上层匹配
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for chain_key, tokens in _BUILTIN_TOKENLIST.items():
        result[chain_key] = {
            contract.lower(): info for contract, info in tokens.items()
        }
    return result


# ---------------------------------------------------------------------------
# 符号推导 (contract address → BASE-QUOTE)
# ---------------------------------------------------------------------------

def _symbol_for_token(
    symbol: str, chain: str, base_currency: str = "USD"
) -> str:
    """将代币符号映射为 Yahoo Finance 风格代码 (BASE-QUOTE)."""
    base = symbol.upper()
    quote = base_currency.upper()
    return f"{base}-{quote}"


# ---------------------------------------------------------------------------
# 核心: import_wallet
# ---------------------------------------------------------------------------

def import_wallet(
    chain: str,
    address: str,
    *,
    tokenlist: str | None = None,
    base_currency: str = "USD",
    rpc_timeout: float = 12.0,
) -> dict[str, Any]:
    """查询某 EVM 链地址的余额, 返回可直接写入 portfolio.json 的结构.

    Parameters
    ----------
    chain : str
        链标识 (eth / bsc / polygon / arbitrum / avalanche).
    address : str
        EVM 公钥/地址 (0x + 40 hex chars).
    tokenlist : str | None
        外部 tokenlist JSON 路径; None 使用内置列表.
    base_currency : str
        计价货币 (默认 USD).
    rpc_timeout : float
        单次 RPC 超时秒数 (默认 12).

    Returns
    -------
    dict
        {
            "chain": str,
            "address": str,
            "holdings": [
                {"symbol": "ETH-USD", "quantity": 1.5, "contract": None, "source": "native"},
                {"symbol": "USDT-USD", "quantity": 100.0, "contract": "0xdAC17F...", "source": "erc20"},
                ...
            ],
            "errors": ["..."]
        }
    """
    errors: list[str] = []
    addr = _validate_address(address, chain)
    chain_cfg = _CHAIN_CONFIG[chain.lower()]
    token_data = _load_tokenlist(tokenlist)
    chain_tokens = token_data.get(chain.lower(), {})

    holdings: list[dict[str, Any]] = []

    # 1. 原生代币
    native = None
    try:
        native = _native_balance(addr, chain, chain_cfg, rpc_timeout)
    except RuntimeError as e:
        errors.append(f"原生代币余额查询失败: {e}")

    if native and native["amount"] > 0:
        holdings.append({
            "symbol": _symbol_for_token(native["symbol"], chain, base_currency),
            "quantity": native["amount"],
            "contract": None,
            "source": "native",
            "chain": chain,
        })

    # 2. ERC-20 代币 (单个失败不阻断其余)
    seen: set[str] = set()
    for contract, info in chain_tokens.items():
        if contract in seen:
            continue
        seen.add(contract)
        try:
            token_balance = _erc20_balance(addr, contract, chain, info["decimals"], rpc_timeout)
        except RuntimeError as e:
            errors.append(f"{info['symbol']} ({contract[:10]}…) 查询失败: {e}")
            continue
        if token_balance and token_balance["amount"] > 0:
            token_balance["symbol"] = info["symbol"]
            holdings.append({
                "symbol": _symbol_for_token(info["symbol"], chain, base_currency),
                "quantity": token_balance["amount"],
                "contract": contract,
                "source": "erc20",
                "chain": chain,
            })

    return {
        "chain": chain,
        "address": addr,
        "holdings": holdings,
        "errors": errors,
    }
