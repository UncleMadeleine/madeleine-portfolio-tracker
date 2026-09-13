"""行情路由离线测试: 加密货币与股票混查时的分流与降级 (provider 层)."""
import types

import pandas as pd
import pytest

from tracker.providers import crypto as crypto_mod
from tracker.providers.base import Quote, resolve
from tracker.providers.orchestration import get_quotes
from tracker.symbols import Market, parse


def _q(sym, price, ccy="USD", name=None):
    return Quote(symbol=sym, name=name or sym, price=price, prev_close=price,
                 change_pct=0.0, currency=ccy)


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """每个测试用临时 DB 隔离, 并清掉 get_quotes 的缓存命中路径."""
    monkeypatch.setattr("tracker.cache.CACHE_DB", tmp_path / "test_quotes_cache.db")
    import tracker.cache as cache_mod
    cache_mod._ensure_db()
    import tracker.providers.orchestration as orch
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda q: None)


def test_get_quotes_mixed_crypto_and_stocks_split_routing(monkeypatch):
    """混合查询: crypto 走本域 provider 逐个取, 股票走批量; 两域互不干扰."""
    import tracker.providers.orchestration as orch

    batch_called, crypto_fetched = [], []

    def fake_batch(parsed):
        batch_called.extend(p.yahoo for p in parsed)
        return {"AAPL": _q("AAPL", 150.0), "0700.HK": _q("0700.HK", 330.0, ccy="HKD")}

    def fake_crypto_quote(p):
        crypto_fetched.append(p.yahoo)
        return _q(p.yahoo, 60000.0)

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(crypto_mod, "_binance_quote", fake_crypto_quote)

    quotes, errors, notes = get_quotes(["AAPL", "0700.HK", "BTC-USD", "ETH-USD"])

    assert errors == {} and notes == []
    assert set(quotes) == {"AAPL", "0700.HK", "BTC-USD", "ETH-USD"}
    # crypto 不进股票批量管道
    assert "BTC-USD" not in batch_called and "ETH-USD" not in batch_called
    assert set(crypto_fetched) == {"BTC-USD", "ETH-USD"}
    assert quotes["BTC-USD"].price == 60000.0


def test_get_quotes_crypto_failure_does_not_block_stocks(monkeypatch):
    """crypto 数据源失败只记 errors, 股票批量照常返回."""
    import tracker.providers.orchestration as orch

    def fake_batch(parsed):
        return {"AAPL": _q("AAPL", 150.0)}

    def failing_crypto(p):
        raise RuntimeError(f"{p.yahoo}: Binance 无有效最新价")

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(crypto_mod, "_binance_quote", failing_crypto)
    monkeypatch.setattr(crypto_mod, "_hl_quote", failing_crypto)
    monkeypatch.setattr(crypto_mod, "_yf_quote", failing_crypto)

    quotes, errors, notes = get_quotes(["AAPL", "BTC-USD"])

    assert quotes["AAPL"].price == 150.0
    assert "BTC-USD" in errors and "Binance" in errors["BTC-USD"]


def test_crypto_domain_routing_and_suffixes():
    """三域代码互不冲突: 裸代码/点后缀=股票, 连字符计价货币=加密货币."""
    assert resolve(parse("AAPL").market).name == "global"
    assert resolve(parse("0700.HK").market).name == "global"
    assert resolve(parse("600519.SS").market).name == "cn"
    assert resolve(parse("200012.SZ").market).name == "cn"  # B股同属 CN 域
    assert resolve(parse("BTC-USD").market).name == "crypto"
    assert parse("BTC-USD").market is Market.CRYPTO
    assert parse("FOO-USD").market is Market.CRYPTO  # 连字符+法币 → crypto 域
    # 同一字符串不会同时映射两个域: AAPL 是美股, 不是 crypto
    assert parse("AAPL").market is Market.US


def test_crypto_quote_route_binance_first():
    """crypto 源链固定 Binance → Hyperliquid (仅USD系) → yfinance."""
    p = parse("BTC-USD")
    provider = resolve(p.market)
    route = provider.quote_sources(p)
    assert route[0] is crypto_mod._binance_quote
    assert route[1] is crypto_mod._hl_quote
    assert route[2] is crypto_mod._yf_quote


def test_binance_quote_from_ticker(monkeypatch):
    """Binance ticker: lastPrice/prevClosePrice/priceChangePercent → Quote."""
    fake = {
        "lastPrice": "61000.5",
        "prevClosePrice": "60000.0",
        "priceChangePercent": "1.6674",
    }
    monkeypatch.setattr(crypto_mod, "_binance_spot_raw", lambda pair: fake)

    q = crypto_mod._binance_quote(parse("BTC-USD"))
    assert q.symbol == "BTC-USD"
    assert q.price == 61000.5
    assert q.prev_close == 60000.0
    assert q.currency == "USD"
    assert q.change_pct == pytest.approx(1.6674)


def test_binance_quote_missing_price_raises(monkeypatch):
    """Binance 无有效价格时抛错, 供降级到 yfinance."""
    monkeypatch.setattr(
        crypto_mod, "_binance_spot_raw", lambda pair: {"lastPrice": "0"}
    )
    with pytest.raises(RuntimeError, match="无有效最新价"):
        crypto_mod._binance_quote(parse("BTC-USD"))


def test_binance_pair_mapping():
    """Binance 交易对映射: USD→USDT, 原币计价保留."""
    assert crypto_mod.binance_pair(parse("BTC-USD")) == "BTCUSDT"
    assert crypto_mod.binance_pair(parse("BTC-USDT")) == "BTCUSDT"
    assert crypto_mod.binance_pair(parse("ETH-BTC")) == "ETHBTC"


def _fake_yf(last, prev=None):
    """构造带 fast_info 的假 yfinance 模块."""
    info = {
        "lastPrice": last,
        "previousClose": prev,
        "yearChange": 0.8,
        "currency": "USD",
    }

    class FakeInfo:
        def get(self, key, default=None):
            return info.get(key, default)

    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "BTC-USD"

        @property
        def fast_info(self):
            return FakeInfo()

    return types.SimpleNamespace(Ticker=FakeTicker)


def test_yf_crypto_quote_from_fast_info(monkeypatch):
    """yfinance 直连: 从 fast_info 取 lastPrice, 日内涨跌由 last/prev 推算."""
    monkeypatch.setattr(crypto_mod, "_yf", lambda: _fake_yf(61000.0, 60000.0))

    q = crypto_mod._yf_quote(parse("BTC-USD"))
    assert q.price == 61000.0
    assert q.prev_close == 60000.0
    assert q.change_pct == pytest.approx((61000 / 60000 - 1) * 100)
    assert q.currency == "USD"


def test_yf_crypto_quote_missing_price_raises(monkeypatch):
    """yfinance 直连无 lastPrice 时抛错."""
    monkeypatch.setattr(crypto_mod, "_yf", lambda: _fake_yf(None))

    with pytest.raises(RuntimeError, match="无最新价"):
        crypto_mod._yf_quote(parse("BTC-USD"))


def test_binance_history_shape(monkeypatch):
    """Binance klines → 标准列名升序 DataFrame."""
    klines = [
        [1788912000000, "78455.8", "79760.0", "77770.0", "78306.43", "14129.92"],
        [1788998400000, "78306.43", "78564.39", "76464.0", "76568.72", "15320.37"],
    ]
    monkeypatch.setattr(crypto_mod, "_get", lambda path, params=None: klines)

    df = crypto_mod._binance_history(parse("BTC-USD"), "2026-09-05", "2026-09-07")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 78306.43


def _fake_hl_ctx(coin="BTC", mark="61000.5", prev="60000.0"):
    """构造 HL metaAndAssetCtxs 假响应: [{"universe": [...]}, [ctx, ...]]."""
    universe = {"universe": [{"name": coin}, {"name": "ETH"}]}
    ctxs = [{"markPx": mark, "prevDayPx": prev}, {"markPx": "2400.0", "prevDayPx": "2350.0"}]
    return [universe, ctxs]


def test_hl_quote_from_asset_ctxs(monkeypatch):
    """HL 永续行情: markPx 现价 + prevDayPx 昨收 → Quote (单请求)."""
    monkeypatch.setattr(crypto_mod, "_hl_post", lambda payload: _fake_hl_ctx())

    q = crypto_mod._hl_quote(parse("BTC-USD"))
    assert q.symbol == "BTC-USD"
    assert q.price == 61000.5
    assert q.prev_close == 60000.0
    assert q.change_pct == pytest.approx((61000.5 / 60000.0 - 1) * 100)
    assert q.currency == "USD"


def test_hl_quote_unknown_coin_raises(monkeypatch):
    """HL 永续无此币种时抛错, 供降级到 yfinance."""
    monkeypatch.setattr(crypto_mod, "_hl_post", lambda payload: _fake_hl_ctx())

    with pytest.raises(RuntimeError, match="无此币种"):
        crypto_mod._hl_quote(parse("FOO-USD"))


def test_hl_quote_non_usd_quote_skips(monkeypatch):
    """非USD系计价 (如 ETH-BTC) 直接拒绝, 不发请求."""
    called = []
    monkeypatch.setattr(crypto_mod, "_hl_post", lambda payload: called.append(payload))

    with pytest.raises(ValueError, match="非USD系计价"):
        crypto_mod._hl_quote(parse("ETH-BTC"))
    assert called == []


def test_hl_history_shape(monkeypatch):
    """HL candleSnapshot → 标准列名升序 DataFrame."""
    bars = [
        {"t": 1788912000000, "o": "78455.8", "h": "79760.0", "l": "77770.0", "c": "78306.43", "v": "14129.92"},
        {"t": 1788998400000, "o": "78306.43", "h": "78564.39", "l": "76464.0", "c": "76568.72", "v": "15320.37"},
    ]
    monkeypatch.setattr(crypto_mod, "_hl_post", lambda payload: bars)

    df = crypto_mod._hl_history(parse("BTC-USD"), "2026-09-05", "2026-09-07")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 78306.43
    assert df["date"].iloc[0] < df["date"].iloc[1]


def test_crypto_quote_falls_back_to_hyperliquid(monkeypatch):
    """Binance 失败 → HL 接管; 记录的 errors 为空."""
    import tracker.providers.orchestration as orch

    def failing_binance(p):
        raise RuntimeError("Binance API 不可达")

    monkeypatch.setattr(crypto_mod, "_binance_quote", failing_binance)
    monkeypatch.setattr(crypto_mod, "_hl_quote", lambda p: _q(p.yahoo, 61000.5))

    quotes, errors, notes = get_quotes(["BTC-USD"])
    assert quotes["BTC-USD"].price == 61000.5
    assert errors == {}
