"""链上钱包历史余额: 回放算法 / 各浏览器源解析 (全部网络 mock)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tracker import wallet_history as wh


ADDR = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
TRON_ADDR = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
SOL_ADDR = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _ts(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp())


def _eth() -> wh.Asset:
    return wh._native_asset("eth")


class TestReconstruct:
    def test_reverse_from_current_balance(self):
        native = _eth()
        usdc = wh._token_asset("eth", "0xusdc", "USDC", 6)
        ledger = wh.Ledger(
            chain="eth",
            address=ADDR,
            assets={native.key: native, usdc.key: usdc},
            current={native.key: 10.0, usdc.key: 0.0},
            transfers=[
                wh.Transfer(_ts("2026-01-01"), native.key, 5.0),
                wh.Transfer(_ts("2026-01-02"), native.key, -2.0),
                wh.Transfer(_ts("2026-01-02"), usdc.key, 50.0),
                wh.Transfer(_ts("2026-01-03"), usdc.key, -50.0),
            ],
        )
        days, balances, issues = wh.reconstruct_daily_balances(
            ledger, date(2026, 1, 1), date(2026, 1, 3)
        )
        assert [d.isoformat() for d in days] == [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
        ]
        assert balances[native.key] == [12.0, 10.0, 10.0]
        assert balances[usdc.key] == [0.0, 50.0, 0.0]
        assert issues == []

    def test_outside_window_ignored(self):
        native = _eth()
        ledger = wh.Ledger(
            chain="eth",
            address=ADDR,
            assets={native.key: native},
            current={native.key: 1.0},
            transfers=[
                wh.Transfer(_ts("2025-12-31"), native.key, 99.0),
                wh.Transfer(_ts("2026-01-02"), native.key, 1.0),
            ],
        )
        _days, balances, _issues = wh.reconstruct_daily_balances(
            ledger, date(2026, 1, 1), date(2026, 1, 2)
        )
        assert balances[native.key] == [0.0, 1.0]

    def test_negative_clamped_with_issue(self):
        native = _eth()
        ledger = wh.Ledger(
            chain="eth",
            address=ADDR,
            assets={native.key: native},
            current={native.key: 0.0},
            transfers=[wh.Transfer(_ts("2026-01-02"), native.key, -1.0)],
        )
        _days, balances, issues = wh.reconstruct_daily_balances(
            ledger, date(2026, 1, 1), date(2026, 1, 2)
        )
        assert balances[native.key] == [1.0, 0.0]
        assert issues == []

        # 当前余额不足以解释窗口内的转入: 反推起点为负, 截到 0 并给出问题提示
        ledger.current[native.key] = 0.0
        ledger.transfers = [wh.Transfer(_ts("2026-01-02"), native.key, 1.0)]
        _days, balances, issues = wh.reconstruct_daily_balances(
            ledger, date(2026, 1, 1), date(2026, 1, 2)
        )
        assert balances[native.key] == [0.0, 0.0]
        assert issues and "负余额" in issues[0]


class TestConfig:
    def test_env_key_overrides(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "wallet_history.json"
        cfg_file.write_text(
            '{"explorer_key": "from-file", "max_pages": 3}', encoding="utf-8"
        )
        monkeypatch.setenv("TRACKER_WALLET_HISTORY_CONFIG", str(cfg_file))
        monkeypatch.setenv("TRACKER_EXPLORER_KEY", "from-env")
        wh.reset()
        cfg = wh.load_config()
        assert cfg["explorer_key"] == "from-env"
        assert cfg["max_pages"] == 3

    def test_malformed_config_falls_back(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "wallet_history.json"
        cfg_file.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("TRACKER_WALLET_HISTORY_CONFIG", str(cfg_file))
        monkeypatch.delenv("TRACKER_EXPLORER_KEY", raising=False)
        wh.reset()
        cfg = wh.load_config()
        assert cfg["max_pages"] == wh.DEFAULTS["max_pages"]
        assert cfg["explorer_key"] == ""

    def test_scrub_hides_api_key(self):
        wh._SECRETS.add("secret-key-123")
        try:
            msg = wh._scrub("http://x?apikey=secret-key-123&foo=1")
            assert "secret-key-123" not in msg
            assert "apikey=***" in msg
        finally:
            wh._SECRETS.discard("secret-key-123")


# ---------------------------------------------------------------------------
# Routescan
# ---------------------------------------------------------------------------


class TestRoutescan:
    def _fake_http(self, url, params=None, **_kw):
        if url.endswith("/gas-balance"):
            return {"items": [{"balance": "2000000000000000000"}]}
        if url.endswith("/erc20-holdings"):
            return {
                "items": [
                    {
                        "tokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "tokenSymbol": "USDC",
                        "tokenDecimals": 6,
                        "tokenQuantity": "100000000",
                    }
                ],
                "link": {},
            }
        if url.endswith("/erc20-transfers"):
            return {
                "items": [
                    {
                        "txHash": "0xaaa",
                        "timestamp": "2026-01-02T12:00:00Z",
                        "from": "0xother",
                        "to": ADDR,
                        "tokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "tokenSymbol": "USDC",
                        "tokenDecimals": 6,
                        "amount": "50000000",
                    }
                ],
                "link": {},
            }
        if url.endswith("/transactions"):
            return {
                "items": [
                    {
                        "id": "0xin",
                        "timestamp": "2026-01-02T01:00:00Z",
                        "from": "0xother",
                        "to": ADDR,
                        "value": "1000000000000000000",
                        "gasUsed": "21000",
                        "gasPrice": "1000000000",
                        "status": True,
                    },
                    {
                        "id": "0xout",
                        "timestamp": "2026-01-03T01:00:00Z",
                        "from": ADDR,
                        "to": "0xother",
                        "value": "500000000000000000",
                        "gasUsed": "21000",
                        "gasPrice": "1000000000",
                        "status": True,
                    },
                ],
                "link": {},
            }
        if url.endswith("/internal-operations"):
            return {
                "items": [
                    {
                        "txHash": "0xint",
                        "timestamp": "2026-01-02T02:00:00Z",
                        "from": "0xcontract",
                        "to": ADDR,
                        "value": "250000000000000000",
                        "status": True,
                    }
                ],
                "link": {},
            }
        raise AssertionError(url)

    def test_normalizes_holdings_transfers_and_fees(self, monkeypatch):
        monkeypatch.setattr(wh, "_http_json", self._fake_http)
        ledger = wh.fetch_ledger(
            "eth",
            ADDR,
            date(2026, 1, 1),
            date(2026, 1, 3),
            cfg={**wh.DEFAULTS, "max_pages": 5},
        )
        assert ledger.providers == ["Routescan"]
        assert ledger.current["native"] == pytest.approx(2.0)
        usdc_key = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        assert ledger.current[usdc_key] == pytest.approx(100.0)
        # 收入 1 ETH + 内部转入 0.25 ETH; 转出 0.5 ETH + 0.000021 ETH 手续费
        deltas = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert sum(deltas) == pytest.approx(1.0 + 0.25 - 0.5 - 0.000021)
        token_deltas = [t.delta for t in ledger.transfers if t.asset_key == usdc_key]
        assert token_deltas == [pytest.approx(50.0)]


    def test_internal_root_operation_is_not_double_counted(self, monkeypatch):
        """Routescan internal-operations 的 opIndex=0 是交易根调用, 与 txlist 重复."""
        one_eth = "1000000000000000000"

        def fake(url, params=None, **_kw):
            if url.endswith("/gas-balance"):
                return {"items": [{"balance": "0"}]}
            if url.endswith("/erc20-holdings"):
                return {"items": [], "link": {}}
            if url.endswith("/erc20-transfers"):
                return {"items": [], "link": {}}
            if url.endswith("/transactions"):
                return {
                    "items": [
                        {
                            "id": "0x1",
                            "timestamp": "2026-01-02T01:00:00Z",
                            "from": "0xother",
                            "to": ADDR,
                            "value": one_eth,
                            "gasUsed": "0",
                            "gasPrice": "0",
                            "status": True,
                        }
                    ],
                    "link": {},
                }
            if url.endswith("/internal-operations"):
                return {
                    "items": [
                        {
                            "opIndex": 0,
                            "txHash": "0x1",
                            "timestamp": "2026-01-02T01:00:00Z",
                            "from": "0xother",
                            "to": ADDR,
                            "value": one_eth,
                            "status": True,
                        }
                    ],
                    "link": {},
                }
            raise AssertionError(url)

        monkeypatch.setattr(wh, "_http_json", fake)
        ledger = wh._fetch_evm_routescan(
            1,
            "eth",
            ADDR,
            _ts("2026-01-01"),
            _ts("2026-01-03", 23),
            {**wh.DEFAULTS, "max_pages": 2},
        )
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert sum(native) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Blockscout
# ---------------------------------------------------------------------------


class TestBlockscout:
    def test_fee_object_is_deducted(self, monkeypatch):
        """Blockscout v2 的 fee 是对象; 解析成 0 会漏掉所有转出 gas."""
        fee_wei = "3400145656320"

        def fake(url, params=None, **_kw):
            if url.endswith("/token-balances"):
                return []
            if "/token-transfers" in url:
                return {"items": [], "next_page_params": None}
            if url.endswith("/transactions"):
                return {
                    "items": [
                        {
                            "hash": "0xout",
                            "timestamp": "2026-01-02T01:00:00Z",
                            "from": {"hash": ADDR},
                            "to": {"hash": "0xother"},
                            "value": "0",
                            "fee": {"type": "actual", "value": fee_wei},
                            "status": "ok",
                        }
                    ],
                    "next_page_params": None,
                }
            if url.endswith("/internal-transactions"):
                return {"items": [], "next_page_params": None}
            if url.rstrip("/").endswith(f"/addresses/{ADDR}"):
                return {"coin_balance": "0"}
            raise AssertionError(url)

        monkeypatch.setattr(wh, "_http_json", fake)
        ledger = wh._fetch_evm_blockscout(
            "https://example.invalid",
            "eth",
            ADDR,
            _ts("2026-01-01"),
            _ts("2026-01-03", 23),
            {**wh.DEFAULTS, "max_pages": 2},
        )
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert native == [pytest.approx(-int(fee_wei) / 1e18)]


# ---------------------------------------------------------------------------
# Etherscan 兼容接口
# ---------------------------------------------------------------------------


class TestEtherscanLike:
    def _fake_http(self, url, params=None, **_kw):
        action = (params or {}).get("action")
        if action == "balance":
            return {"status": "1", "result": "2000000000000000000"}
        if action == "addresstokenbalance":
            return {
                "status": "1",
                "result": [
                    {
                        "TokenAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "TokenSymbol": "USDC",
                        "TokenDivisor": "6",
                        "TokenQuantity": "3000000",
                    }
                ],
            }
        if action == "tokentx":
            return {
                "status": "1",
                "result": [
                    {
                        "hash": "0xt",
                        "timeStamp": str(_ts("2026-01-02")),
                        "from": "0xother",
                        "to": ADDR,
                        "contractAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "tokenSymbol": "USDC",
                        "tokenDecimal": "6",
                        "value": "1000000",
                    }
                ],
            }
        if action == "txlist":
            return {
                "status": "1",
                "result": [
                    {
                        "hash": "0xn",
                        "timeStamp": str(_ts("2026-01-02")),
                        "from": ADDR,
                        "to": "0xother",
                        "value": "1000000000000000000",
                        "gasUsed": "21000",
                        "gasPrice": "1000000000",
                        "isError": "0",
                    }
                ],
            }
        if action == "txlistinternal":
            return {"status": "0", "message": "No transactions found", "result": []}
        if action == "tokenbalance":
            return {"status": "1", "result": "3000000"}
        raise AssertionError(action)

    def test_normalizes_and_handles_empty_internal(self, monkeypatch):
        monkeypatch.setattr(wh, "_http_json", self._fake_http)
        ledger = wh._fetch_evm_etherscan_like(
            "https://example/api",
            "eth",
            ADDR,
            _ts("2026-01-01"),
            _ts("2026-01-03", 23),
            {**wh.DEFAULTS, "max_pages": 3},
            label="TestScan",
        )
        assert ledger.current["native"] == pytest.approx(2.0)
        usdc_key = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        assert ledger.current[usdc_key] == pytest.approx(3.0)
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert native == [pytest.approx(-1.0 - 0.000021)]
        assert [t.delta for t in ledger.transfers if t.asset_key == usdc_key] == [
            pytest.approx(1.0)
        ]


# ---------------------------------------------------------------------------
# TRON
# ---------------------------------------------------------------------------


class TestTron:
    def test_transfers_and_current_balances(self, monkeypatch):
        trc20_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

        def fake_get(path, cfg):
            if path.startswith("/v1/accounts/") and "/transactions/trc20" in path:
                return {
                    "data": [
                        {
                            "transaction_id": "tin",
                            "block_timestamp": _ts("2026-01-02") * 1000,
                            "from": "Tother",
                            "to": TRON_ADDR,
                            "token_info": {
                                "address": trc20_contract,
                                "symbol": "USDT",
                                "decimals": 6,
                            },
                            "value": "5000000",
                        }
                    ],
                    "meta": {},
                }
            if path.startswith("/v1/accounts/") and "/transactions" in path:
                hex_addr = wh._tron_hex_address(TRON_ADDR)
                return {
                    "data": [
                        {
                            "txID": "tout",
                            "block_timestamp": _ts("2026-01-03") * 1000,
                            "raw_data": {
                                "contract": [
                                    {
                                        "type": "TransferContract",
                                        "parameter": {
                                            "value": {
                                                "owner_address": hex_addr,
                                                "to_address": "41" + "11" * 20,
                                                "amount": 2000000,
                                            }
                                        },
                                    }
                                ]
                            },
                            "ret": [{"fee": 100000}],
                        }
                    ],
                    "meta": {},
                }
            if path.startswith("/v1/accounts/"):
                return {
                    "data": [
                        {
                            "balance": 10000000,
                            "trc20": [{trc20_contract: "5000000"}],
                        }
                    ]
                }
            raise AssertionError(path)

        monkeypatch.setattr(wh, "_tron_get", fake_get)
        ledger = wh.fetch_ledger(
            "tron",
            TRON_ADDR,
            date(2026, 1, 1),
            date(2026, 1, 3),
            cfg=wh.DEFAULTS,
        )
        assert ledger.current["native"] == pytest.approx(10.0)
        key = wh._token_key("tron", trc20_contract)
        assert ledger.current[key] == pytest.approx(5.0)
        assert [t.delta for t in ledger.transfers if t.asset_key == key] == [
            pytest.approx(5.0)
        ]
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert native == [pytest.approx(-(2.0 + 0.1))]

    def test_failed_transfer_only_counts_fee(self, monkeypatch):
        hex_addr = wh._tron_hex_address(TRON_ADDR)

        def fake_get(path, cfg):
            if path.startswith("/v1/accounts/") and "/transactions/trc20" in path:
                return {"data": [], "meta": {}}
            if path.startswith("/v1/accounts/") and "/transactions" in path:
                return {
                    "data": [
                        {
                            "txID": "tfail",
                            "block_timestamp": _ts("2026-01-02") * 1000,
                            "raw_data": {
                                "contract": [
                                    {
                                        "type": "TransferContract",
                                        "parameter": {
                                            "value": {
                                                "owner_address": hex_addr,
                                                "to_address": "41" + "11" * 20,
                                                "amount": 2000000,
                                            }
                                        },
                                    }
                                ]
                            },
                            "ret": [
                                {"fee": 100000, "contractRet": "REVERT"}
                            ],
                        }
                    ],
                    "meta": {},
                }
            if path.startswith("/v1/accounts/"):
                return {"data": [{"balance": 0, "trc20": []}]}
            raise AssertionError(path)

        monkeypatch.setattr(wh, "_tron_get", fake_get)
        ledger = wh.fetch_ledger(
            "tron",
            TRON_ADDR,
            date(2026, 1, 1),
            date(2026, 1, 3),
            cfg=wh.DEFAULTS,
        )
        # 失败转账的 2 TRX 并未移动, 只应扣 0.1 TRX 手续费
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert native == [pytest.approx(-0.1)]


# ---------------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------------


class TestSolana:
    def test_transaction_balance_deltas(self, monkeypatch):
        bar = int(wh._day_start_ts(date(2026, 1, 2)) + 3600)
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

        def fake_rpc(method, params, cfg):
            if method == "getBalance":
                return {"value": 2_000_000_000}
            if method == "getTokenAccountsByOwner":
                if (
                    params[1].get("programId")
                    != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
                ):
                    return {"value": []}
                return {
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": usdc_mint,
                                            "tokenAmount": {
                                                "amount": "5000000",
                                                "decimals": 6,
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            if method == "getSignaturesForAddress":
                if params[1].get("before"):
                    return []
                return [
                    {
                        "signature": "sig1",
                        "blockTime": bar,
                        "err": None,
                    }
                ]
            if method == "getTransaction":
                return {
                    "blockTime": bar,
                    "meta": {
                        "preBalances": [0, 2_500_000_000],
                        "postBalances": [0, 2_000_000_000],
                        "preTokenBalances": [
                            {
                                "owner": SOL_ADDR,
                                "mint": usdc_mint,
                                "uiTokenAmount": {"amount": "5000000", "decimals": 6},
                            }
                        ],
                        "postTokenBalances": [
                            {
                                "owner": SOL_ADDR,
                                "mint": usdc_mint,
                                "uiTokenAmount": {"amount": "7000000", "decimals": 6},
                            }
                        ],
                    },
                    "transaction": {
                        "message": {
                            "accountKeys": [
                                {"pubkey": "other"},
                                {"pubkey": SOL_ADDR},
                            ]
                        }
                    },
                }
            raise AssertionError(method)

        monkeypatch.setattr(wh, "_sol_rpc", fake_rpc)
        ledger = wh.fetch_ledger(
            "solana",
            SOL_ADDR,
            date(2026, 1, 2),
            date(2026, 1, 2),
            cfg={**wh.DEFAULTS, "max_pages": 2, "max_transactions": 10},
        )
        assert ledger.current["native"] == pytest.approx(2.0)
        native = [t.delta for t in ledger.transfers if t.asset_key == "native"]
        assert native == [pytest.approx(-0.5)]
        token = [t for t in ledger.transfers if t.asset_key == usdc_mint]
        assert [t.delta for t in token] == [pytest.approx(2.0)]

    def test_wsol_is_merged_into_native(self, monkeypatch):
        wsol = "So11111111111111111111111111111111111111112"

        def fake_rpc(method, params, cfg):
            if method == "getBalance":
                return {"value": 1_000_000_000}
            if method == "getTokenAccountsByOwner":
                if (
                    params[1].get("programId")
                    != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
                ):
                    return {"value": []}
                return {
                    "value": [
                        {
                            "account": {
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": wsol,
                                            "tokenAmount": {
                                                "amount": "1000000000",
                                                "decimals": 9,
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            if method == "getSignaturesForAddress":
                return []
            raise AssertionError(method)

        monkeypatch.setattr(wh, "_sol_rpc", fake_rpc)
        ledger = wh.fetch_ledger(
            "solana",
            SOL_ADDR,
            date(2026, 1, 2),
            date(2026, 1, 2),
            cfg={**wh.DEFAULTS, "max_pages": 2, "max_transactions": 10},
        )
        assert ledger.current["native"] == pytest.approx(2.0)
        assert wsol not in ledger.assets
        assert any("wSOL" in note for note in ledger.notes)
