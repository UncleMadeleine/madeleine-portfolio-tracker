"""链上钱包余额查询: 轻钱包策略 (内置 tokenlist + 只读余额调用).

支持 EVM 链 (ETH / BSC / Polygon / Arbitrum / Avalanche)、TRON 与 Solana:
  - EVM: 主币 eth_getBalance + ERC-20 balanceOf 批量调用
  - TRON: TronGrid HTTP API GET /v1/accounts/{addr} (一次返回 TRX + 全部 TRC-20)
  - Solana: getBalance + getTokenAccountsByOwner jsonParsed 取回 SOL + 全部 SPL
    (SPL 查询是索引调用, 部分公共节点收费; 可用 TRACKER_SOLANA_RPC 指定节点)
  - 内置主流代币 tokenlist; 支持 --tokenlist 加载外部 JSON 覆盖

不依赖 web3.py/tronpy/solana-py, 直接 HTTP (requests); 无需 API key.
公钥/地址只读查询, 不涉及私钥操作.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
        "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI", "decimals": 18},
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC", "decimals": 8},
        "0x514910771AF9Ca656af840dff83E8264EcF986CA": {
            "symbol": "LINK",
            "decimals": 18,
        },
        "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984": {"symbol": "UNI", "decimals": 18},
        "0x7fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9": {
            "symbol": "AAVE",
            "decimals": 18,
        },
        "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE": {
            "symbol": "SHIB",
            "decimals": 18,
        },
        "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84": {
            "symbol": "stETH",
            "decimals": 18,
        },
        "0xaea46A60368A7bD060eec7DF8CBa43b7EF41Ad85": {"symbol": "FET", "decimals": 18},
        "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32": {"symbol": "LDO", "decimals": 18},
        "0xBBbbCA6A901c926F240b89EacB641d8Aec7AEafD": {"symbol": "LRC", "decimals": 18},
        "0xfB7B4564402E5500dB5bB6d63Ae671302777C75a": {
            "symbol": "DEXT",
            "decimals": 18,
        },
    },
    "bsc": {
        "0x55d398326f99059fF775485246999027B3197955": {
            "symbol": "USDT",
            "decimals": 18,
        },
        "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d": {
            "symbol": "USDC",
            "decimals": 18,
        },
        "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3": {"symbol": "DAI", "decimals": 18},
        "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c": {
            "symbol": "BTCB",
            "decimals": 18,
        },
        "0x2170Ed0880ac9A755fd29B2688956BD959F933F8": {"symbol": "ETH", "decimals": 18},
        "0x2dfF88A56767223A5529eA5960Da7A3F5f766406": {"symbol": "ID", "decimals": 18},
        "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82": {
            "symbol": "CAKE",
            "decimals": 18,
        },
    },
    "polygon": {
        "0xc2132D05D31c914a87C6611C10748AEb04B58e8F": {"symbol": "USDT", "decimals": 6},
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174": {"symbol": "USDC", "decimals": 6},
        "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063": {"symbol": "DAI", "decimals": 18},
        "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6": {"symbol": "WBTC", "decimals": 8},
        "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619": {"symbol": "ETH", "decimals": 18},
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270": {
            "symbol": "WMATIC",
            "decimals": 18,
        },
        "0xb33EaAd8d922B1083446DC23f610c2567fB5180F": {"symbol": "UNI", "decimals": 18},
    },
    "arbitrum": {
        "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9": {"symbol": "USDT", "decimals": 6},
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": {"symbol": "USDC", "decimals": 6},
        "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1": {"symbol": "DAI", "decimals": 18},
        "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f": {"symbol": "WBTC", "decimals": 8},
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": {
            "symbol": "WETH",
            "decimals": 18,
        },
        "0x5979D7b546E38E414F7E9822514be443A4800529": {
            "symbol": "wstETH",
            "decimals": 18,
        },
        "0x912CE59144191C1204E64559FE8253a0e49E6548": {"symbol": "ARB", "decimals": 18},
        "0xB766039cc6DB368759C1E56B79AFfE831d0Cc507": {"symbol": "RPL", "decimals": 18},
    },
    "avalanche": {
        "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7": {"symbol": "USDT", "decimals": 6},
        "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E": {"symbol": "USDC", "decimals": 6},
        "0xd586E7F844cEa2F87f50152665BCbc2C279D8d70": {"symbol": "DAI", "decimals": 18},
        "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c": {"symbol": "WBTC", "decimals": 8},
        "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB": {
            "symbol": "WETH",
            "decimals": 18,
        },
        "0x6e84a6216eA6dACC71eE8E6b0a5B7322EEbC0fDd": {"symbol": "JOE", "decimals": 18},
    },
    "tron": {
        # 合约地址为 base58 形式 (区分大小写); symbol 经 Tronscan 核对
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": {"symbol": "USDT", "decimals": 6},
        "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": {"symbol": "USDC", "decimals": 6},
        "TUpMhErZL2fhh4sVNULAbNKLokS4GjC1F4": {"symbol": "TUSD", "decimals": 18},
        "TCFLL5dx5ZJdKnWuesXxi1VPwjLVmWZZy9": {"symbol": "JST", "decimals": 18},
        "TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4": {"symbol": "BTT", "decimals": 18},
        "TSSMHYeV2uE9qYH95DqyoCuNCzEL1NvU3S": {"symbol": "SUN", "decimals": 18},
        "TMwFHYXLJaRUPeW6421aqXL4ZEzPRFGkGT": {"symbol": "USDJ", "decimals": 18},
        "TFczxzPhnThNSqr5by8tvxsdCFRRz6cPNq": {"symbol": "NFT", "decimals": 6},
        "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7": {"symbol": "WIN", "decimals": 6},
    },
    "solana": {
        # mint 地址为 base58 32 字节公钥 (区分大小写); decimals 经 getTokenSupply 核对;
        # WSOL 不在此列, 导入时并入原生 SOL (见 _import_solana)
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {"symbol": "USDC", "decimals": 6},
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": {"symbol": "USDT", "decimals": 6},
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": {"symbol": "JUP", "decimals": 6},
        "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": {"symbol": "BONK", "decimals": 5},
        "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": {
            "symbol": "JITOSOL",
            "decimals": 9,
        },
        "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": {"symbol": "PYTH", "decimals": 6},
        "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {"symbol": "RAY", "decimals": 6},
        "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": {"symbol": "JTO", "decimals": 9},
        "EKpQGSJtjMFqKZ9KQanSqYXRcF8gtDCb9MvaJrDipump": {"symbol": "WIF", "decimals": 6},
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
    "tron": {
        "label": "TRON",
        "native_symbol": "TRX",
        "native_decimals": 6,
        # TronGrid 兼容 HTTP API (GET /v1/accounts/{address}), 非 JSON-RPC
        "rpc": [
            "https://api.trongrid.io",
            "https://api.tronstack.io",
        ],
        "tokenlist": "tron",
        "kind": "tron",
    },
    "solana": {
        "label": "Solana",
        "native_symbol": "SOL",
        "native_decimals": 9,
        # 公共 JSON-RPC; getTokenAccountsByOwner 是索引调用, 部分节点付费,
        # TRACKER_SOLANA_RPC 环境变量可前置一个自定义节点 (按序尝试)
        "rpc": [
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com",
            "https://solana.api.onfinality.io/public",
        ],
        "tokenlist": "solana",
        "kind": "solana",
    },
}

_SUPPORTED_CHAINS = set(_CHAIN_CONFIG)

# ERC-20 balanceOf 签名
_ERC20_BALANCE_SIG = bytes.fromhex("70a08231")
_ERC20_BALANCE_TOPIC = "0x" + _ERC20_BALANCE_SIG.hex()

# TRON base58check 地址 ("T" 开头 34 字符, 25 字节载荷 + 4 字节 checksum)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_TRON_ADDR_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

# Solana 账户公钥: base58, 解码后恰为 32 字节
_SOL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_SOL_TOKEN2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Wrapped SOL mint: 等价于 SOL, 导入时并入原生 SOL 数量而非单独计价
_SOL_WSOL_MINT = "So11111111111111111111111111111111111111112"

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
        r = req.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code == 200:
            body = r.json()
            if "result" in body:
                return body["result"]
            err = body.get("error", {})
            raise RuntimeError(
                f"RPC 错误 [{err.get('code')}]: {err.get('message', body)}"
            )
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

def _tron_get(hosts: list[str], path: str, timeout: float = 10.0) -> Any:
    """GET TronGrid 兼容接口, 依次尝试多个节点, 任一成功返回 JSON."""
    req = _requests()
    last_err: Exception | None = None
    for host in hosts:
        try:
            r = req.get(host.rstrip("/") + path, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"全部 TRON 节点不可达 ({last_err})")


# ---------------------------------------------------------------------------
# 地址校验
# ---------------------------------------------------------------------------


def _b58_decode(addr: str) -> bytes | None:
    """base58 解码 → 原始字节; 非法字符/超长返回 None."""
    num = 0
    try:
        for ch in addr:
            num = num * 58 + _B58_INDEX[ch]
    except KeyError:
        return None
    body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    # base58 前导 "1" 编码为 0x00 字节
    return b"\x00" * (len(addr) - len(addr.lstrip("1"))) + body

def _b58check_decode_tron(addr: str) -> bytes | None:
    """base58check 解码 TRON 地址 → 21 字节载荷 (0x41 + 20B); 校验失败返回 None."""
    if not _TRON_ADDR_RE.fullmatch(addr):
        return None
    body = _b58_decode(addr)
    if body is None or len(body) != 25:
        return None
    payload, checksum = body[:-4], body[-4:]
    if payload[0] != 0x41:
        return None
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    if digest[:4] != checksum:
        return None
    return payload

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
    if chain_cfg.get("kind") == "tron":
        # base58 区分大小写, 校验通过后原样返回 (不 lower)
        if _b58check_decode_tron(addr) is None:
            raise ValueError(
                f"地址格式无效 (TRON 地址需 base58check 的 'T' 开头 34 字符): "
                f"{addr[:10]}..."
            )
        return addr
    if chain_cfg.get("kind") == "solana":
        # base58 公钥 (区分大小写), 解码恰为 32 字节; 原样返回 (不 lower)
        decoded = _b58_decode(addr)
        if decoded is None or len(decoded) != 32:
            raise ValueError(
                f"地址格式无效 (Solana 地址需 base58 32 字节公钥, "
                f"通常 32-44 字符): {addr[:10]}..."
            )
        return addr
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise ValueError(f"地址格式无效 (需 0x + 40 位十六进制字符): {addr[:10]}...")
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
    amount = raw_wei / (10**decimals)
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
    amount = raw / (10**decimals)
    return {
        "symbol": None,  # 由调用方从 tokenlist 填入
        "contract": contract,
        "amount": amount,
        "decimals": decimals,
        "chain": chain,
        "source": "erc20",
    }

def _tron_account(
    address: str, chain_cfg: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """GET /v1/accounts/{address} → 账户对象; 未激活账户返回 {}."""
    body = _tron_get(chain_cfg["rpc"], f"/v1/accounts/{address}", timeout)
    data = body.get("data") or []
    return data[0] if data else {}


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
            # 链名统一小写: 上层按 chain.lower() 查表, 外部文件写 "ETH" 会静默查不到
            chain_l = chain_key.lower()
            loaded[chain_l] = {}
            for contract, info in tokens.items():
                # EVM 合约不区分大小写统一小写; TRON base58 地址区分大小写, 原样保留
                ckey = contract.lower() if contract.startswith("0x") else contract
                loaded[chain_l][ckey] = {
                    "symbol": info["symbol"],
                    "decimals": info["decimals"],
                }
        return loaded
    # 内置列表: EVM 混合-case 合约归一化为小写; TRON base58 地址保留原样
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for chain_key, tokens in _BUILTIN_TOKENLIST.items():
        result[chain_key] = {
            (contract.lower() if contract.startswith("0x") else contract): info
            for contract, info in tokens.items()
        }
    return result


# ---------------------------------------------------------------------------
# 符号推导 (contract address → BASE-QUOTE)
# ---------------------------------------------------------------------------


def _symbol_for_token(symbol: str, chain: str, base_currency: str = "USD") -> str:
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
    """查询链上地址余额 (EVM / TRON / Solana), 返回可直接写入 portfolio.json 的结构.

    Parameters
    ----------
    chain : str
        链标识 (eth / bsc / polygon / arbitrum / avalanche / tron / solana).
    address : str
        EVM 地址 (0x + 40 hex)、TRON 地址 (base58check, "T" 开头 34 字符)
        或 Solana 地址 (base58, 32 字节公钥).
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
    # 链名归一化: _CHAIN_CONFIG 键全小写, 大写/混合大小写链名 ("ETH") 会让
    # _erc20_balance 的 _CHAIN_CONFIG[chain] KeyError; 入口统一 lower 一次
    chain = str(chain).strip().lower()
    if chain not in _SUPPORTED_CHAINS:
        raise ValueError(
            f"不支持的链: {chain}. 支持: {', '.join(sorted(_SUPPORTED_CHAINS))}"
        )
    errors: list[str] = []
    addr = _validate_address(address, chain)
    chain_cfg = _CHAIN_CONFIG[chain]
    token_data = _load_tokenlist(tokenlist)
    chain_tokens = token_data.get(chain, {})

    if chain_cfg.get("kind") == "tron":
        return _import_tron(chain, addr, chain_cfg, chain_tokens, base_currency,
                            rpc_timeout)
    if chain_cfg.get("kind") == "solana":
        return _import_solana(chain, addr, chain_cfg, chain_tokens, base_currency,
                              rpc_timeout)

    holdings: list[dict[str, Any]] = []

    # 1. 原生代币
    native = None
    try:
        native = _native_balance(addr, chain, chain_cfg, rpc_timeout)
    except RuntimeError as e:
        errors.append(f"原生代币余额查询失败: {e}")

    if native and native["amount"] > 0:
        holdings.append(
            {
                "symbol": _symbol_for_token(native["symbol"], chain, base_currency),
                "quantity": native["amount"],
                "contract": None,
                "source": "native",
                "chain": chain,
            }
        )

    # 2. ERC-20 代币 (单个失败不阻断其余)
    seen: set[str] = set()
    for contract, info in chain_tokens.items():
        if contract in seen:
            continue
        seen.add(contract)
        try:
            token_balance = _erc20_balance(
                addr, contract, chain, info["decimals"], rpc_timeout
            )
        except RuntimeError as e:
            errors.append(f"{info['symbol']} ({contract[:10]}…) 查询失败: {e}")
            continue
        if token_balance and token_balance["amount"] > 0:
            token_balance["symbol"] = info["symbol"]
            holdings.append(
                {
                    "symbol": _symbol_for_token(info["symbol"], chain, base_currency),
                    "quantity": token_balance["amount"],
                    "contract": contract,
                    "source": "erc20",
                    "chain": chain,
                }
            )

    return {
        "chain": chain,
        "address": addr,
        "holdings": holdings,
        "errors": errors,
    }


def _import_tron(
    chain: str,
    address: str,
    chain_cfg: dict[str, Any],
    chain_tokens: dict[str, dict[str, Any]],
    base_currency: str,
    timeout: float,
) -> dict[str, Any]:
    """TRON 导入: 单次 /v1/accounts 调用取回 TRX 原生余额 + 全部 TRC-20."""
    holdings: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        account = _tron_account(address, chain_cfg, timeout)
    except RuntimeError as e:
        errors.append(f"TRON 账户查询失败: {e}")
        account = {}

    # 原生 TRX 总量 (与 TronLink 口径一致) = 可用 balance
    #   + frozenV2 自质押 (ENERGY/BANDWIDTH 条目有 amount; TRON_POWER 是投票权
    #     标记无 amount, 跳过)
    #   + 已委托质押 account_resource.delegated_frozenV2_balance_for_energy /
    #     delegated_frozenV2_balance_for_bandwidth (DelegateResource 转出的 TRX,
    #     所有权仍在原地址)
    #   + unfrozen 解冻中 + canWithdrawUnfreezeAmount 已满14天可领取
    #   + Stake1.0 遗留 frozen / account_resource.frozen_balance_for_energy
    # 未激活/零余额无字段; 单位 sun (1 TRX = 1e6 sun)
    def _to_sun(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    spendable_sun = _to_sun(account.get("balance"))
    staked_sun = sum(
        _to_sun(item.get("amount"))
        for item in account.get("frozenV2") or []
        if isinstance(item, dict)
    )
    staked_sun += _to_sun(account.get("delegated_frozenV2_balance_for_bandwidth"))
    res = account.get("account_resource")
    if isinstance(res, dict):
        staked_sun += _to_sun(res.get("frozen_balance_for_energy"))
        staked_sun += _to_sun(res.get("delegated_frozenV2_balance_for_energy"))
    staked_sun += sum(
        _to_sun(item.get("frozen_balance"))
        for item in account.get("frozen") or []
        if isinstance(item, dict)
    )
    unstaking_sun = sum(
        _to_sun(item.get("unfreeze_amount"))
        for item in account.get("unfrozen") or []
        if isinstance(item, dict)
    )
    unstaking_sun += _to_sun(account.get("canWithdrawUnfreezeAmount"))
    trx_sun = spendable_sun + staked_sun + unstaking_sun
    if trx_sun > 0:
        holdings.append(
            {
                "symbol": _symbol_for_token(
                    chain_cfg["native_symbol"], chain, base_currency
                ),
                "quantity": trx_sun / (10 ** chain_cfg["native_decimals"]),
                "contract": None,
                "source": "native",
                "chain": chain,
            }
        )
    notes = []
    if staked_sun > 0:
        notes.append(f"质押 {staked_sun / 1e6:g}")
    if unstaking_sun > 0:
        notes.append(f"解冻中 {unstaking_sun / 1e6:g}")
    if notes:
        errors.append(
            f"TRX 合计含 {' + '.join(notes)} TRX (已计入数量, 当前不可转账)"
        )

    # TRC-20: trc20 字段为 [{contract: raw_amount_str}, ...], 只纳入 tokenlist
    # 内的代币 (Tron 上 air-drop 代币泛滥, 无 symbol/decimals 元数据的合约无法定价)
    balances: dict[str, int] = {}
    for item in account.get("trc20") or []:
        if not isinstance(item, dict):
            continue
        for contract, raw in item.items():
            try:
                balances[contract] = balances.get(contract, 0) + int(raw)
            except (TypeError, ValueError):
                continue

    unknown: list[str] = []
    for contract, raw_amount in balances.items():
        if raw_amount <= 0:
            continue
        info = chain_tokens.get(contract)
        if info is None:
            unknown.append(contract)
            continue
        holdings.append(
            {
                "symbol": _symbol_for_token(info["symbol"], chain, base_currency),
                "quantity": raw_amount / (10 ** info["decimals"]),
                "contract": contract,
                "source": "trc20",
                "chain": chain,
            }
        )
    if unknown:
        skipped = ", ".join(c[:12] + "…" for c in unknown[:5])
        errors.append(
            f"{len(unknown)} 个 TRC-20 代币不在 tokenlist, 已跳过: {skipped}"
        )

    return {
        "chain": chain,
        "address": address,
        "holdings": holdings,
        "errors": errors,
    }


def _solana_rpc_hosts(chain_cfg: dict[str, Any]) -> list[str]:
    """Solana RPC 节点列表: TRACKER_SOLANA_RPC (逗号分隔) 前置自定义节点."""
    custom = [
        h.strip()
        for h in os.environ.get("TRACKER_SOLANA_RPC", "").split(",")
        if h.strip()
    ]
    return custom + list(chain_cfg["rpc"])


def _import_solana(
    chain: str,
    address: str,
    chain_cfg: dict[str, Any],
    chain_tokens: dict[str, dict[str, Any]],
    base_currency: str,
    timeout: float,
) -> dict[str, Any]:
    """Solana 导入: getBalance 取原生 SOL, getTokenAccountsByOwner(jsonParsed)
    一次取回全部 SPL 代币账户 (Token Program + Token-2022 各查一次).

    getTokenAccountsByOwner 是索引调用, 部分公共节点收费/限速; SPL 部分失败时
    原生 SOL 仍返回, 详情记 errors. Wrapped SOL (So111...112) 并入 SOL 数量.
    """
    holdings: list[dict[str, Any]] = []
    errors: list[str] = []
    hosts = _solana_rpc_hosts(chain_cfg)

    # 1. 原生 SOL (lamports → SOL)
    sol_lamports = 0
    try:
        res = _try_rpc_hosts(
            hosts,
            {"jsonrpc": "2.0", "id": 1, "method": "getBalance",
             "params": [address]},
            timeout,
        )
        sol_lamports = int((res or {}).get("value") or 0)
    except RuntimeError as e:
        errors.append(f"SOL 余额查询失败: {e}")

    # 2. SPL 代币: 同一 owner 可能有多个 token account, 按 mint 累加
    #    (如散户地址常见的 dust 账户); decimals 以链上为准, tokenlist 兜底
    by_mint: dict[str, dict[str, Any]] = {}
    for program_id in (_SOL_TOKEN_PROGRAM, _SOL_TOKEN2022_PROGRAM):
        try:
            res = _try_rpc_hosts(
                hosts,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        address,
                        {"programId": program_id},
                        {"encoding": "jsonParsed"},
                    ],
                },
                timeout,
            )
        except RuntimeError as e:
            errors.append(f"SPL 代币账户查询失败 ({program_id[:8]}…): {e}")
            continue
        for item in (res or {}).get("value") or []:
            try:
                info = item["account"]["data"]["parsed"]["info"]
                ta = info["tokenAmount"]
                raw = int(ta["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            if raw <= 0:
                continue
            mint = info.get("mint") or ""
            if mint not in by_mint:
                by_mint[mint] = {"raw": 0, "decimals": ta.get("decimals")}
            by_mint[mint]["raw"] += raw

    # WSOL 并入原生 SOL (Wrapped SOL ≈ SOL, 单独计价会与 SOL-USD 合并时互相覆盖)
    wsol = by_mint.pop(_SOL_WSOL_MINT, None)
    wsol_lamports = int(wsol["raw"]) if wsol else 0
    sol_total = sol_lamports + wsol_lamports
    if sol_total > 0:
        holdings.append(
            {
                "symbol": _symbol_for_token(
                    chain_cfg["native_symbol"], chain, base_currency
                ),
                "quantity": sol_total / (10 ** chain_cfg["native_decimals"]),
                "contract": None,
                "source": "native",
                "chain": chain,
            }
        )
    if wsol_lamports > 0:
        errors.append(
            f"SOL 合计含 wSOL {wsol_lamports / 1e9:g} (已计入数量)"
        )

    # 只纳入 tokenlist 内的代币 (Solana 上空投 meme 泛滥, 无元数据的 mint 无法定价)
    unknown: list[str] = []
    for mint, bal in by_mint.items():
        info = chain_tokens.get(mint)
        if info is None:
            unknown.append(mint)
            continue
        decimals = bal["decimals"] if bal["decimals"] is not None else info["decimals"]
        holdings.append(
            {
                "symbol": _symbol_for_token(info["symbol"], chain, base_currency),
                "quantity": bal["raw"] / (10 ** decimals),
                "contract": mint,
                "source": "spl",
                "chain": chain,
            }
        )
    if unknown:
        skipped = ", ".join(c[:12] + "…" for c in unknown[:5])
        errors.append(
            f"{len(unknown)} 个 SPL 代币不在 tokenlist, 已跳过: {skipped}"
        )

    return {
        "chain": chain,
        "address": address,
        "holdings": holdings,
        "errors": errors,
    }
