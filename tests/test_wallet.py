"""链上钱包余额查询测试 (全部 RPC 调用 mock, 无需网络)."""

from __future__ import annotations

import json
import re

import pytest

from tracker import wallet as wallet_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_requests(
    responses: list[dict] | None = None, always_error: bool = False
):
    """构造假的 requests 模块.

    responses: 从列表中按顺序返回 (每个 RPC 调用消耗一个).
    always_error: 全部抛 ConnectionError.
    """

    class FakeResp:
        def __init__(self, result):
            self._result = result
            self.status_code = 200
            self.text = json.dumps(result) if isinstance(result, dict) else str(result)

        def json(self):
            return self._result

    class _FakeRequester:
        def __init__(self, resps, fail):
            self._resps = list(resps or [])
            self._fail = fail
            self._idx = 0

        def _next(self, url):
            if self._fail:
                raise ConnectionError(f"mock RPC error for {url[:40]}")
            if self._idx < len(self._resps):
                r = self._resps[self._idx]
                self._idx += 1
                return FakeResp(r)
            raise ConnectionError(
                f"no more mock responses (idx={self._idx}, url={url[:40]})"
            )

        def post(self, url, json=None, params=None, timeout=None, headers=None):
            return self._next(url)

        def get(self, url, params=None, timeout=None, headers=None):
            return self._next(url)

    return _FakeRequester(responses or [], always_error)


def _always_zero_requests():
    """始终返回 0 余额的 fake module (适用于测试零余额场景)."""
    return _make_fake_requests([{"result": "0x0"}])


def _per_call_responses(call_results: list[dict]):
    """每个 RPC 调用返回对应值; 超出后抛错 (模拟部分节点不可达)."""
    return _make_fake_requests(call_results)


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


class TestValidateAddress:
    def test_valid_lowercase(self):
        addr = wallet_mod._validate_address(
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "eth"
        )
        assert addr == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

    def test_valid_uppercase(self):
        addr = wallet_mod._validate_address(
            "0xD8DA6BF26964AF9D7EED9E03E53415D37AA96045", "eth"
        )
        assert addr == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

    def test_invalid_prefix(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address("0xABCD", "eth")

    def test_empty(self):
        with pytest.raises(ValueError, match="地址不能为空"):
            wallet_mod._validate_address("", "eth")

    def test_unsupported_chain(self):
        with pytest.raises(ValueError, match="不支持的链"):
            wallet_mod._validate_address(
                "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "cosmos"
            )

    def test_mixed_case_normalized(self):
        addr = "0xd8Da6Bf26964aF9D7eeD9e03E53415D37aA96045"
        result = wallet_mod._validate_address(addr, "eth")
        assert result == result.lower()


# ---------------------------------------------------------------------------
# _SUPPORTED_CHAINS 完整性
# ---------------------------------------------------------------------------


class TestSupportedChains:
    def test_all_chains_in_config(self):
        for chain in wallet_mod._SUPPORTED_CHAINS:
            assert chain in wallet_mod._CHAIN_CONFIG

    def test_each_chain_has_rpc_list(self):
        for chain, cfg in wallet_mod._CHAIN_CONFIG.items():
            assert isinstance(cfg["rpc"], list)
            assert len(cfg["rpc"]) >= 1

    def test_each_chain_has_native_symbol(self):
        for chain, cfg in wallet_mod._CHAIN_CONFIG.items():
            assert cfg["native_symbol"], f"{chain} missing native_symbol"
            assert cfg["native_decimals"] >= 0

    def test_tokenlist_keys_match_config(self):
        for chain, cfg in wallet_mod._CHAIN_CONFIG.items():
            tl_key = cfg.get("tokenlist")
            if tl_key:
                assert tl_key in wallet_mod._BUILTIN_TOKENLIST, (
                    f"chain {chain} tokenlist key '{tl_key}' not found"
                )

    def test_tokenlist_addresses_well_formed(self):
        for chain, tokens in wallet_mod._BUILTIN_TOKENLIST.items():
            for contract, info in tokens.items():
                if chain == "tron":  # base58 合约地址 (区分大小写)
                    assert re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", contract), (
                        f"{chain} {contract}"
                    )
                elif chain == "solana":  # base58 mint 公钥, 解码恰为 32 字节
                    decoded = wallet_mod._b58_decode(contract)
                    assert decoded is not None and len(decoded) == 32, (
                        f"{chain} {contract}"
                    )
                else:
                    assert re.fullmatch(r"0x[0-9a-fA-F]{40}", contract), (
                        f"{chain} {contract}"
                    )
                assert info["decimals"] >= 0, f"{chain} {contract}"

    def test_tokenlist_symbols_are_valid_crypto_bases(self):
        """代币符号会拼成 BASE-QUOTE 写入 portfolio.json, 必须是 parse 可识别的 crypto 代码."""
        from tracker.symbols import Market, parse

        for chain, tokens in wallet_mod._BUILTIN_TOKENLIST.items():
            for contract, info in tokens.items():
                code = wallet_mod._symbol_for_token(info["symbol"], chain)
                p = parse(code)
                assert p.market is Market.CRYPTO, f"{chain} {contract} → {code}"
                assert p.currency == "USD", f"{chain} {contract} → {code}"


# ---------------------------------------------------------------------------
# _native_balance
# ---------------------------------------------------------------------------


class TestNativeBalance:
    def test_returns_none_for_zero(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _always_zero_requests(),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "eth",
            wallet_mod._CHAIN_CONFIG["eth"],
            10.0,
        )
        assert result is None

    def test_returns_balance_for_positive(self, monkeypatch):
        # 1 ETH = 1e18 wei
        one_eth_wei = hex(10**18)
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([{"result": one_eth_wei}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "eth",
            wallet_mod._CHAIN_CONFIG["eth"],
            10.0,
        )
        assert result is not None
        assert result["symbol"] == "ETH"
        assert result["amount"] == pytest.approx(1.0)
        assert result["contract"] is None
        assert result["source"] == "native"

    def test_fractional_amount(self, monkeypatch):
        # 0.5 ETH = 0.5e18 wei
        half_eth = hex(int(0.5 * 10**18))
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([{"result": half_eth}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "eth",
            wallet_mod._CHAIN_CONFIG["eth"],
            10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(0.5)

    def test_all_hosts_fail_raises(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests(always_error=True),
        )
        with pytest.raises(RuntimeError, match="全部 RPC 节点不可达"):
            wallet_mod._native_balance(
                "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
                "eth",
                wallet_mod._CHAIN_CONFIG["eth"],
                2.0,
            )

    def test_bsc_native_balance(self, monkeypatch):
        one_bnb = hex(10**18)
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([{"result": one_bnb}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "bsc",
            wallet_mod._CHAIN_CONFIG["bsc"],
            10.0,
        )
        assert result is not None
        assert result["symbol"] == "BNB"
        assert result["amount"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _erc20_balance
# ---------------------------------------------------------------------------


class TestERC20Balance:
    USDT_CONTRACT = "0xdac17f958d2ee523a2206206994597c13d831ec7"

    def test_returns_none_for_zero(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _always_zero_requests(),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            self.USDT_CONTRACT,
            "eth",
            6,
            10.0,
        )
        assert result is None

    def test_returns_balance(self, monkeypatch):
        # 100 USDT = 100 * 1e6 = 100000000
        raw = hex(100_000_000)
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([{"result": raw}]),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            self.USDT_CONTRACT,
            "eth",
            6,
            10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(100.0)
        assert result["symbol"] is None  # 由调用方填充
        assert result["contract"] == self.USDT_CONTRACT.lower()

    def test_decimals_applied(self, monkeypatch):
        # 1 WBTC = 1 * 1e8
        raw = hex(10**8)
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([{"result": raw}]),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "0x2260fac5e5542a773aa44fbfcedf7c193bc2c599",
            "eth",
            8,
            10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(1.0)

    def test_all_hosts_fail_raises(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests(always_error=True),
        )
        with pytest.raises(RuntimeError, match="全部 RPC 节点不可达"):
            wallet_mod._erc20_balance(
                "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
                self.USDT_CONTRACT,
                "eth",
                6,
                2.0,
            )

    @pytest.mark.parametrize(
        "payload", [{"result": "not-hex"}, {"result": None}, {"result": ""}]
    )
    def test_malformed_result_returns_none(self, monkeypatch, payload):
        """节点返回非 hex / 空载荷时按无余额处理, 不得抛 ValueError 给调用方."""
        monkeypatch.setattr(
            wallet_mod,
            "_requests",
            lambda: _make_fake_requests([payload]),
        )
        assert (
            wallet_mod._erc20_balance(
                "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
                self.USDT_CONTRACT,
                "eth",
                6,
                10.0,
            )
            is None
        )


# ---------------------------------------------------------------------------
# import_wallet integration
# ---------------------------------------------------------------------------


class TestImportWallet:
    def test_returns_holdings_with_native_and_token(self, monkeypatch):
        """同时有主币和 ERC-20 时, holdings 都包含且符号正确."""
        responses = [hex(10**18), hex(100_000_000)]
        idx = [0]

        def fake_try_rpc_hosts(hosts, payload, timeout):
            r = responses[idx[0] % len(responses)]
            idx[0] += 1
            return r

        monkeypatch.setattr(wallet_mod, "_try_rpc_hosts", fake_try_rpc_hosts)
        result = wallet_mod.import_wallet(
            "eth",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            base_currency="USD",
        )
        assert result["chain"] == "eth"
        assert result["address"] == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        symbols = [h["symbol"] for h in result["holdings"]]
        assert "ETH-USD" in symbols
        assert "USDT-USD" in symbols
        native = next(h for h in result["holdings"] if h["source"] == "native")
        assert native["quantity"] == pytest.approx(1.0)

    def test_skips_zero_balances(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            lambda hosts, payload, timeout: "0x0",
        )
        result = wallet_mod.import_wallet(
            "eth",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        )
        assert result["holdings"] == []

    def test_malformed_payload_does_not_abort_import(self, monkeypatch):
        """节点返回非 hex 载荷时跳过该代币, 整个导入不得崩掉 (曾抛 ValueError 逃出 except)."""
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            lambda hosts, payload, timeout: "not-hex",
        )
        result = wallet_mod.import_wallet(
            "eth",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        )
        assert result["holdings"] == []
        assert result["errors"] == []

    def test_unsupported_chain_raises(self):
        with pytest.raises(ValueError, match="不支持的链"):
            wallet_mod.import_wallet(
                "cosmos", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
            )

    def test_invalid_address_raises(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod.import_wallet("eth", "not-an-address")

    def test_chain_name_case_insensitive(self, monkeypatch):
        """大写/混合大小写链名归一化为小写: _CHAIN_CONFIG 键全小写,
        不归一会让 _erc20_balance 的 _CHAIN_CONFIG[chain] KeyError."""
        seen_chains = []

        def fake_erc20(addr, contract, chain, decimals, timeout):
            seen_chains.append(chain)
            return None  # 零余额 → 不产出持仓

        monkeypatch.setattr(wallet_mod, "_native_balance", lambda *a: None)
        monkeypatch.setattr(wallet_mod, "_erc20_balance", fake_erc20)
        result = wallet_mod.import_wallet(
            "ETH", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        )
        assert result["chain"] == "eth"
        assert seen_chains and set(seen_chains) == {"eth"}

    def test_contract_field_set_for_erc20(self, monkeypatch):
        """ERC-20 条目含 contract 字段."""

        def fake_native(*args, **kwargs):
            return None

        def fake_erc20(address, contract, chain, decimals, timeout):
            return {
                "amount": 42.0,
                "symbol": "USDT",
                "contract": contract,
                "chain": "eth",
                "source": "erc20",
            }

        monkeypatch.setattr(wallet_mod, "_native_balance", fake_native)
        monkeypatch.setattr(wallet_mod, "_erc20_balance", fake_erc20)
        result = wallet_mod.import_wallet(
            "eth", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        )
        erc20s = [h for h in result["holdings"] if h["source"] == "erc20"]
        assert len(erc20s) >= 1
        assert erc20s[0]["contract"] is not None
        assert erc20s[0]["contract"].startswith("0x")

    def test_bsc_chain_works(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            lambda hosts, payload, timeout: hex(10**18),
        )
        result = wallet_mod.import_wallet(
            "bsc",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            base_currency="USD",
        )
        assert result["chain"] == "bsc"
        symbols = [h["symbol"] for h in result["holdings"]]
        assert "BNB-USD" in symbols

    def test_external_tokenlist_overrides_builtin(self, monkeypatch, tmp_path):
        custom = {
            "eth": {
                "0xAbC1234567890aBcdef1234567890aBcDeF12345": {
                    "symbol": "CUSTOM",
                    "decimals": 18,
                }
            }
        }
        tl_file = tmp_path / "custom_tokens.json"
        tl_file.write_text(json.dumps(custom), encoding="utf-8")

        def fake_native(*args, **kwargs):
            return None

        def fake_erc20(address, contract, chain, decimals, timeout):
            if contract.lower() == "0xabc1234567890abcdef1234567890abcdef12345":
                return {
                    "amount": 42.0,
                    "symbol": "CUSTOM",
                    "contract": contract,
                    "chain": "eth",
                }
            return None

        monkeypatch.setattr(wallet_mod, "_native_balance", fake_native)
        monkeypatch.setattr(wallet_mod, "_erc20_balance", fake_erc20)

        result = wallet_mod.import_wallet(
            "eth",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            tokenlist=str(tl_file),
        )
        symbols = [h["symbol"] for h in result["holdings"]]
        assert "CUSTOM-USD" in symbols
        assert "USDT-USD" not in symbols

    def test_polygon_chain(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            lambda hosts, payload, timeout: hex(10**18),
        )
        result = wallet_mod.import_wallet(
            "polygon",
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        )
        assert result["chain"] == "polygon"
        symbols = [h["symbol"] for h in result["holdings"]]
        assert "MATIC-USD" in symbols


# ---------------------------------------------------------------------------
# _load_tokenlist
# ---------------------------------------------------------------------------


class TestLoadTokenlist:
    def test_builtin_has_eth_key(self):
        tl = wallet_mod._load_tokenlist(None)
        assert "eth" in tl
        assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in tl["eth"]

    def test_external_file_loaded(self, monkeypatch, tmp_path):
        custom = {"bsc": {"0xABCD": {"symbol": "FOO", "decimals": 18}}}
        p = tmp_path / "tl.json"
        p.write_text(json.dumps(custom), encoding="utf-8")
        tl = wallet_mod._load_tokenlist(str(p))
        assert "bsc" in tl
        assert "0xabcd" in tl["bsc"]

    def test_external_file_normalizes_keys_lower(self, monkeypatch, tmp_path):
        custom = {
            "eth": {
                "0xDAC17F958D2ee523a2206206994597C13D831ec7": {
                    "symbol": "USDT",
                    "decimals": 6,
                }
            }
        }
        p = tmp_path / "tl.json"
        p.write_text(json.dumps(custom), encoding="utf-8")
        tl = wallet_mod._load_tokenlist(str(p))
        assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in tl["eth"]

    def test_external_file_normalizes_chain_key_lower(self, tmp_path):
        """链名大小写不敏感: 外部文件写 "ETH" 也要能被 chain.lower() 查到."""
        custom = {
            "ETH": {
                "0xdac17f958d2ee523a2206206994597c13d831ec7": {
                    "symbol": "USDT",
                    "decimals": 6,
                }
            }
        }
        p = tmp_path / "tl.json"
        p.write_text(json.dumps(custom), encoding="utf-8")
        tl = wallet_mod._load_tokenlist(str(p))
        assert "eth" in tl
        assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in tl["eth"]

    def test_missing_external_file_raises(self):
        with pytest.raises(FileNotFoundError):
            wallet_mod._load_tokenlist("/nonexistent/path/tokens.json")


# ---------------------------------------------------------------------------
# _symbol_for_token
# ---------------------------------------------------------------------------


class TestSymbolForToken:
    def test_default_usd(self):
        assert wallet_mod._symbol_for_token("ETH", "eth") == "ETH-USD"

    def test_custom_currency(self):
        assert wallet_mod._symbol_for_token("USDT", "eth", "EUR") == "USDT-EUR"

    def test_lowercase_normalized(self):
        result = wallet_mod._symbol_for_token("eth", "eth")
        assert result == "ETH-USD"

# ---------------------------------------------------------------------------
# TRON 链 (base58check 地址 + TronGrid /v1/accounts)
# ---------------------------------------------------------------------------


class TestTronWallet:
    ADDR = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
    USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def test_validate_address_valid(self):
        assert wallet_mod._validate_address(self.ADDR, "tron") == self.ADDR

    def test_validate_address_bad_checksum(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address(
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeX", "tron"
            )

    def test_validate_address_evm_rejected(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address(
                "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "tron"
            )

    def test_import_native_and_trc20(self, monkeypatch):
        body = {
            "data": [
                {
                    "balance": 150_000_000,  # 150 TRX (sun)
                    "trc20": [
                        {self.USDT: "250000000"},          # 250 USDT (6 位)
                        {"TMwFHYXLJaRUPeW6421aqXL4ZEzPRFGkGT": "0"},
                    ],
                }
            ]
        }
        monkeypatch.setattr(
            wallet_mod, "_tron_get", lambda hosts, path, timeout: body
        )
        result = wallet_mod.import_wallet("tron", self.ADDR)
        assert result["chain"] == "tron"
        assert result["address"] == self.ADDR
        assert result["errors"] == []
        by_symbol = {h["symbol"]: h for h in result["holdings"]}
        assert by_symbol["TRX-USD"]["quantity"] == pytest.approx(150.0)
        assert by_symbol["TRX-USD"]["source"] == "native"
        assert by_symbol["USDT-USD"]["quantity"] == pytest.approx(250.0)
        assert by_symbol["USDT-USD"]["source"] == "trc20"

    def test_staked_and_unstaking_trx_merged(self, monkeypatch):
        """frozenV2(质押中) + unfrozen(解冻中) 计入 TRX 总量; TRON_POWER 无 amount 跳过."""
        body = {
            "data": [
                {
                    "balance": 930_279,  # 0.930279 TRX 可用
                    "frozenV2": [
                        {},
                        {"amount": 124_000_000, "type": "ENERGY"},
                        {"type": "TRON_POWER"},
                    ],
                    "unfrozen": [{"unfreeze_amount": 10_000_000}],
                }
            ]
        }
        monkeypatch.setattr(
            wallet_mod, "_tron_get", lambda hosts, path, timeout: body
        )
        result = wallet_mod.import_wallet("tron", self.ADDR)
        trx = next(h for h in result["holdings"] if h["symbol"] == "TRX-USD")
        assert trx["quantity"] == pytest.approx(0.930279 + 124.0 + 10.0)
        assert any("质押 124" in e and "解冻中 10" in e for e in result["errors"])

    def test_unknown_trc20_noted_not_imported(self, monkeypatch):
        body = {"data": [{"trc20": [{"TUnknownContractxxxxxxxxxxxxxxxxxxx": "9"}]}]}
        monkeypatch.setattr(
            wallet_mod, "_tron_get", lambda hosts, path, timeout: body
        )
        result = wallet_mod.import_wallet("tron", self.ADDR)
        assert result["holdings"] == []
        assert result["errors"] and "不在 tokenlist" in result["errors"][0]

    def test_unactivated_account_empty(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod, "_tron_get", lambda hosts, path, timeout: {"data": []}
        )
        result = wallet_mod.import_wallet("tron", self.ADDR)
        assert result["holdings"] == []
        assert result["errors"] == []

    def test_api_failure_reported_in_errors(self, monkeypatch):
        def _fail(hosts, path, timeout):
            raise RuntimeError("全部 TRON 节点不可达")

        monkeypatch.setattr(wallet_mod, "_tron_get", _fail)
        result = wallet_mod.import_wallet("tron", self.ADDR)
        assert result["holdings"] == []
        assert result["errors"] and "TRON 账户查询失败" in result["errors"][0]


# ---------------------------------------------------------------------------
# Solana 链 (base58 地址 + getBalance / getTokenAccountsByOwner)
# ---------------------------------------------------------------------------


class TestSolanaWallet:
    # 系统投票程序地址 (已知有效的 32 字节 base58 公钥)
    ADDR = "Vote111111111111111111111111111111111111111"
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    WSOL_MINT = "So11111111111111111111111111111111111111112"

    @staticmethod
    def _token_account(mint: str, raw: str, decimals: int) -> dict:
        return {
            "pubkey": "x" * 43,
            "account": {
                "data": {
                    "parsed": {
                        "info": {
                            "mint": mint,
                            "tokenAmount": {
                                "amount": raw,
                                "decimals": decimals,
                            },
                        }
                    }
                }
            },
        }

    def _fake_rpc(self, *, lamports=0, token_accounts=None, token2022_accounts=None):
        """按 method+programId 分发的 _try_rpc_hosts mock (参数为 result 层载荷)."""

        def fake(hosts, payload, timeout):
            method = payload["method"]
            if method == "getBalance":
                return {"value": lamports}
            if method == "getTokenAccountsByOwner":
                program = payload["params"][1]["programId"]
                if program == wallet_mod._SOL_TOKEN_PROGRAM:
                    return {"value": token_accounts or []}
                return {"value": token2022_accounts or []}
            raise AssertionError(f"unexpected method {method}")

        return fake

    def test_validate_address_valid(self):
        assert wallet_mod._validate_address(self.ADDR, "solana") == self.ADDR

    def test_validate_address_bad_base58(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address("0xZZ" + self.ADDR, "solana")

    def test_validate_address_wrong_length(self):
        # base58 合法但解码不是 32 字节 → 无效
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address("abc", "solana")

    def test_validate_address_evm_rejected(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address(
                "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "solana"
            )

    def test_import_native_and_spl(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            self._fake_rpc(
                lamports=int(2.5e9),
                token_accounts=[self._token_account(self.USDC_MINT, "1500000", 6)],
            ),
        )
        result = wallet_mod.import_wallet("solana", self.ADDR)
        assert result["chain"] == "solana"
        assert result["address"] == self.ADDR
        assert result["errors"] == []
        by_symbol = {h["symbol"]: h for h in result["holdings"]}
        assert by_symbol["SOL-USD"]["quantity"] == pytest.approx(2.5)
        assert by_symbol["SOL-USD"]["source"] == "native"
        assert by_symbol["USDC-USD"]["quantity"] == pytest.approx(1.5)
        assert by_symbol["USDC-USD"]["contract"] == self.USDC_MINT
        assert by_symbol["USDC-USD"]["source"] == "spl"

    def test_multiple_token_accounts_same_mint_summed(self, monkeypatch):
        """同一 owner 的多个 token account 按 mint 累加."""
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            self._fake_rpc(
                token_accounts=[
                    self._token_account(self.USDC_MINT, "1000000", 6),
                    self._token_account(self.USDC_MINT, "500000", 6),
                ],
            ),
        )
        result = wallet_mod.import_wallet("solana", self.ADDR)
        usdc = next(h for h in result["holdings"] if h["symbol"] == "USDC-USD")
        assert usdc["quantity"] == pytest.approx(1.5)

    def test_token2022_accounts_included(self, monkeypatch):
        """Token-2022 程序下的账户也计入 (如 PYTH-USDC 等 2022 代币)."""
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            self._fake_rpc(
                token2022_accounts=[self._token_account(self.USDC_MINT, "2000000", 6)],
            ),
        )
        result = wallet_mod.import_wallet("solana", self.ADDR)
        usdc = next(h for h in result["holdings"] if h["symbol"] == "USDC-USD")
        assert usdc["quantity"] == pytest.approx(2.0)

    def test_wsol_merged_into_native_sol(self, monkeypatch):
        """wSOL 不单独成行, 并入 SOL-USD 数量并记提示."""
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            self._fake_rpc(
                lamports=int(1e9),
                token_accounts=[self._token_account(self.WSOL_MINT, "500000000", 9)],
            ),
        )
        result = wallet_mod.import_wallet("solana", self.ADDR)
        sols = [h for h in result["holdings"] if h["symbol"] == "SOL-USD"]
        assert len(sols) == 1
        assert sols[0]["quantity"] == pytest.approx(1.5)
        assert any("wSOL" in e for e in result["errors"])

    def test_unknown_mint_skipped(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod,
            "_try_rpc_hosts",
            self._fake_rpc(
                token_accounts=[
                    self._token_account("UnknownMint1111111111111111111111111111111", "1", 0)
                ],
            ),
        )
        result = wallet_mod.import_wallet("solana", self.ADDR)
        assert result["holdings"] == []
        assert result["errors"] and "不在 tokenlist" in result["errors"][0]

    def test_rpc_failure_partial_result(self, monkeypatch):
        """SPL 索引调用失败时原生 SOL 仍返回, 失败记 errors."""

        def fake(hosts, payload, timeout):
            if payload["method"] == "getBalance":
                return {"value": int(1e9)}
            raise RuntimeError("全部 RPC 节点不可达")

        monkeypatch.setattr(wallet_mod, "_try_rpc_hosts", fake)
        result = wallet_mod.import_wallet("solana", self.ADDR)
        sols = [h for h in result["holdings"] if h["symbol"] == "SOL-USD"]
        assert sols and sols[0]["quantity"] == pytest.approx(1.0)
        assert any("SPL 代币账户查询失败" in e for e in result["errors"])

    def test_all_rpc_failure(self, monkeypatch):
        def _fail(hosts, payload, timeout):
            raise RuntimeError("全部 RPC 节点不可达")

        monkeypatch.setattr(wallet_mod, "_try_rpc_hosts", _fail)
        result = wallet_mod.import_wallet("solana", self.ADDR)
        assert result["holdings"] == []
        assert result["errors"] and "SOL 余额查询失败" in result["errors"][0]

    def test_solana_rpc_env_override(self, monkeypatch):
        """TRACKER_SOLANA_RPC 前置自定义节点."""
        monkeypatch.setenv("TRACKER_SOLANA_RPC", "https://a.example, https://b.example")
        hosts = wallet_mod._solana_rpc_hosts(wallet_mod._CHAIN_CONFIG["solana"])
        assert hosts[:2] == ["https://a.example", "https://b.example"]
        assert hosts[2:] == wallet_mod._CHAIN_CONFIG["solana"]["rpc"]
