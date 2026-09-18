"""链上钱包余额查询测试 (全部 RPC 调用 mock, 无需网络)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracker import wallet as wallet_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_requests(responses: list[dict] | None = None, always_error: bool = False):
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

        def post(self, url, json=None, params=None, timeout=None, headers=None):
            if self._fail:
                raise ConnectionError(f"mock RPC error for {url[:40]}")
            if self._idx < len(self._resps):
                r = self._resps[self._idx]
                self._idx += 1
                return FakeResp(r)
            raise ConnectionError(
                f"no more mock responses (idx={self._idx}, url={url[:40]})"
            )

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
        addr = wallet_mod._validate_address("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "eth")
        assert addr == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

    def test_valid_uppercase(self):
        addr = wallet_mod._validate_address("0xD8DA6BF26964AF9D7EED9E03E53415D37AA96045", "eth")
        assert addr == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

    def test_invalid_prefix(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod._validate_address("0xABCD", "eth")

    def test_empty(self):
        with pytest.raises(ValueError, match="地址不能为空"):
            wallet_mod._validate_address("", "eth")

    def test_unsupported_chain(self):
        with pytest.raises(ValueError, match="不支持的链"):
            wallet_mod._validate_address("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "solana")

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
                assert tl_key in wallet_mod._BUILTIN_TOKENLIST, f"chain {chain} tokenlist key '{tl_key}' not found"

    def test_tokenlist_addresses_well_formed(self):
        for chain, tokens in wallet_mod._BUILTIN_TOKENLIST.items():
            for contract, info in tokens.items():
                assert re.fullmatch(r"0x[0-9a-fA-F]{40}", contract), f"{chain} {contract}"
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
            wallet_mod, "_requests",
            lambda: _always_zero_requests(),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045", "eth",
            wallet_mod._CHAIN_CONFIG["eth"], 10.0,
        )
        assert result is None

    def test_returns_balance_for_positive(self, monkeypatch):
        # 1 ETH = 1e18 wei
        one_eth_wei = hex(10 ** 18)
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([{"result": one_eth_wei}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045", "eth",
            wallet_mod._CHAIN_CONFIG["eth"], 10.0,
        )
        assert result is not None
        assert result["symbol"] == "ETH"
        assert result["amount"] == pytest.approx(1.0)
        assert result["contract"] is None
        assert result["source"] == "native"

    def test_fractional_amount(self, monkeypatch):
        # 0.5 ETH = 0.5e18 wei
        half_eth = hex(int(0.5 * 10 ** 18))
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([{"result": half_eth}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045", "eth",
            wallet_mod._CHAIN_CONFIG["eth"], 10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(0.5)

    def test_all_hosts_fail_raises(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests(always_error=True),
        )
        with pytest.raises(RuntimeError, match="全部 RPC 节点不可达"):
            wallet_mod._native_balance(
                "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045", "eth",
                wallet_mod._CHAIN_CONFIG["eth"], 2.0,
            )

    def test_bsc_native_balance(self, monkeypatch):
        one_bnb = hex(10 ** 18)
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([{"result": one_bnb}]),
        )
        result = wallet_mod._native_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045", "bsc",
            wallet_mod._CHAIN_CONFIG["bsc"], 10.0,
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
            wallet_mod, "_requests",
            lambda: _always_zero_requests(),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            self.USDT_CONTRACT,
            "eth", 6, 10.0,
        )
        assert result is None

    def test_returns_balance(self, monkeypatch):
        # 100 USDT = 100 * 1e6 = 100000000
        raw = hex(100_000_000)
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([{"result": raw}]),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            self.USDT_CONTRACT,
            "eth", 6, 10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(100.0)
        assert result["symbol"] is None  # 由调用方填充
        assert result["contract"] == self.USDT_CONTRACT.lower()

    def test_decimals_applied(self, monkeypatch):
        # 1 WBTC = 1 * 1e8
        raw = hex(10 ** 8)
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([{"result": raw}]),
        )
        result = wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            "0x2260fac5e5542a773aa44fbfcedf7c193bc2c599",
            "eth", 8, 10.0,
        )
        assert result is not None
        assert result["amount"] == pytest.approx(1.0)

    def test_all_hosts_fail_raises(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests(always_error=True),
        )
        with pytest.raises(RuntimeError, match="全部 RPC 节点不可达"):
            wallet_mod._erc20_balance(
                "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
                self.USDT_CONTRACT,
                "eth", 6, 2.0,
            )

    @pytest.mark.parametrize("payload", [{"result": "not-hex"}, {"result": None}, {"result": ""}])
    def test_malformed_result_returns_none(self, monkeypatch, payload):
        """节点返回非 hex / 空载荷时按无余额处理, 不得抛 ValueError 给调用方."""
        monkeypatch.setattr(
            wallet_mod, "_requests",
            lambda: _make_fake_requests([payload]),
        )
        assert wallet_mod._erc20_balance(
            "0xd8da6bf26964af9d7eed9e03e53415d37aaa96045",
            self.USDT_CONTRACT,
            "eth", 6, 10.0,
        ) is None


# ---------------------------------------------------------------------------
# import_wallet integration
# ---------------------------------------------------------------------------

class TestImportWallet:
    def test_returns_holdings_with_native_and_token(self, monkeypatch):
        """同时有主币和 ERC-20 时, holdings 都包含且符号正确."""
        responses = [hex(10 ** 18), hex(100_000_000)]
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
            wallet_mod, "_try_rpc_hosts",
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
            wallet_mod, "_try_rpc_hosts",
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
            wallet_mod.import_wallet("solana", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")

    def test_invalid_address_raises(self):
        with pytest.raises(ValueError, match="地址格式无效"):
            wallet_mod.import_wallet("eth", "not-an-address")

    def test_contract_field_set_for_erc20(self, monkeypatch):
        """ERC-20 条目含 contract 字段."""

        def fake_native(*args, **kwargs):
            return None

        def fake_erc20(address, contract, chain, decimals, timeout):
            return {
                "amount": 42.0, "symbol": "USDT", "contract": contract,
                "chain": "eth", "source": "erc20",
            }

        monkeypatch.setattr(wallet_mod, "_native_balance", fake_native)
        monkeypatch.setattr(wallet_mod, "_erc20_balance", fake_erc20)
        result = wallet_mod.import_wallet("eth", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        erc20s = [h for h in result["holdings"] if h["source"] == "erc20"]
        assert len(erc20s) >= 1
        assert erc20s[0]["contract"] is not None
        assert erc20s[0]["contract"].startswith("0x")

    def test_bsc_chain_works(self, monkeypatch):
        monkeypatch.setattr(
            wallet_mod, "_try_rpc_hosts",
            lambda hosts, payload, timeout: hex(10 ** 18),
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
                    "symbol": "CUSTOM", "decimals": 18
                }
            }
        }
        tl_file = tmp_path / "custom_tokens.json"
        tl_file.write_text(json.dumps(custom), encoding="utf-8")

        def fake_native(*args, **kwargs):
            return None

        def fake_erc20(address, contract, chain, decimals, timeout):
            if contract.lower() == "0xabc1234567890abcdef1234567890abcdef12345":
                return {"amount": 42.0, "symbol": "CUSTOM", "contract": contract, "chain": "eth"}
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
            wallet_mod, "_try_rpc_hosts",
            lambda hosts, payload, timeout: hex(10 ** 18),
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
        custom = {"eth": {"0xDAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT", "decimals": 6}}}
        p = tmp_path / "tl.json"
        p.write_text(json.dumps(custom), encoding="utf-8")
        tl = wallet_mod._load_tokenlist(str(p))
        assert "0xdac17f958d2ee523a2206206994597c13d831ec7" in tl["eth"]

    def test_external_file_normalizes_chain_key_lower(self, tmp_path):
        """链名大小写不敏感: 外部文件写 "ETH" 也要能被 chain.lower() 查到."""
        custom = {"ETH": {"0xdac17f958d2ee523a2206206994597c13d831ec7": {"symbol": "USDT", "decimals": 6}}}
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
