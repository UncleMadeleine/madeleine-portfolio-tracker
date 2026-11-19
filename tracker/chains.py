"""链元数据注册表: RPC / 原生币 / tokenlist / 浏览器与 EVM chain id.

钱包余额查询 (wallet) 与历史回放 (wallet_history) 共用同一份链配置,
避免两处各写一份地址/节点/浏览器映射后逐渐分叉。
"""

from __future__ import annotations

from typing import Any

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

#: 公开别名 (新代码用 CHAIN_CONFIG; 旧 wallet._CHAIN_CONFIG 仍保留兼容)
CHAIN_CONFIG = _CHAIN_CONFIG
_SUPPORTED_CHAINS = set(_CHAIN_CONFIG)
#: 链代码 → 展示名 (CLI 钱包净值 / 导入页 / 钱包净值页共用)
CHAIN_LABELS = {key: str(cfg["label"]) for key, cfg in _CHAIN_CONFIG.items()}


# EVM 链 → Etherscan V2 / Routescan chain id (历史浏览器查询用)
EVM_CHAIN_ID: dict[str, int] = {
    "eth": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "avalanche": 43114,
}

# Routescan 覆盖的链 (免费公共 Data API)
ROUTESCAN_CHAINS: dict[str, int] = {"eth": 1, "avalanche": 43114}

# Blockscout 默认实例 (无自建/自定义时使用)
DEFAULT_BLOCKSCOUT: dict[str, str] = {
    "eth": "https://eth.blockscout.com",
    "polygon": "https://polygon.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
}

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
SNOWTRACE = "https://api.snowtrace.io/api"
