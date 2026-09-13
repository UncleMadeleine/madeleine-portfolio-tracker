import pytest

from tracker.symbols import Market, normalize, parse


def test_normalize_hk_4digit():
    assert normalize("700.hk") == "0700.HK"
    assert normalize("00700.HK") == "0700.HK"
    assert normalize("80737.HK") == "80737.HK"


def test_normalize_sh_alias():
    assert normalize("600000.SH") == "600000.SS"


def test_parse_us():
    p = parse("aapl")
    assert p.yahoo == "AAPL"
    assert p.market is Market.US
    assert p.currency == "USD"
    assert p.ak_code is None


def test_parse_cn():
    p = parse("600519.SS")
    assert p.market is Market.CN
    assert p.currency == "CNY"
    assert p.ak_code == "600519"
    assert p.is_b_share is False
    p2 = parse("000001.SZ")
    assert p2.market is Market.CN
    assert p2.yahoo == "000001.SZ"
    assert p2.is_b_share is False


def test_parse_cn_b_shares():
    sh_b = parse("900902.SS")
    assert sh_b.market is Market.CN
    assert sh_b.currency == "CNY"
    assert sh_b.ak_code == "900902"
    assert sh_b.is_b_share is True
    assert sh_b.market_label == "B股"
    sz_b = parse("200012.SZ")
    assert sz_b.market is Market.CN
    assert sz_b.currency == "CNY"
    assert sz_b.ak_code == "200012"
    assert sz_b.is_b_share is True
    assert sz_b.market_label == "B股"


def test_parse_cn_sme_not_b_share():
    # 深市中小板 002xxx 是 A 股, 不应误判为 B 股
    p = parse("002594.SZ")
    assert p.market is Market.CN
    assert p.is_b_share is False
    assert p.market_label != "B股"


def test_parse_hk_ak_code_5digit():
    p = parse("0700.HK")
    assert p.market is Market.HK
    assert p.currency == "HKD"
    assert p.ak_code == "00700"


def test_parse_all_markets():
    assert parse("SAP.DE").market is Market.DE
    assert parse("SAP.F").market is Market.DE
    assert parse("BP.L").market is Market.GB
    assert parse("BP.IL").market is Market.GB
    assert parse("RY.TO").market is Market.CA
    assert parse("X.V").market is Market.CA
    assert parse("BHP.AX").market is Market.AU


def test_parse_unknown_suffix():
    with pytest.raises(ValueError):
        parse("FOO.ZZ")


# ---------- 加密货币 ----------


def test_parse_crypto_btc_usd():
    p = parse("BTC-USD")
    assert p.market is Market.CRYPTO
    assert p.yahoo == "BTC-USD"
    assert p.currency == "USD"
    assert p.ak_code is None
    assert p.market_label == "加密货币"


def test_parse_crypto_eth_usd():
    p = parse("ETH-USD")
    assert p.market is Market.CRYPTO
    assert p.currency == "USD"


def test_parse_crypto_btc_eur():
    p = parse("BTC-EUR")
    assert p.market is Market.CRYPTO
    assert p.currency == "EUR"


def test_parse_crypto_no_separator():
    # BTCUSD → BTC-USD (自动补全连字符)
    p = parse("BTCUSD")
    assert p.market is Market.CRYPTO
    assert p.yahoo == "BTC-USD"
    assert p.currency == "USD"


def test_parse_crypto_ethusd_no_separator():
    p = parse("ETHUSD")
    assert p.market is Market.CRYPTO
    assert p.yahoo == "ETH-USD"
    assert p.currency == "USD"


def test_normalize_crypto_no_separator():
    assert normalize("BTCUSD") == "BTC-USD"
    assert normalize("btc-usd") == "BTC-USD"
    assert normalize("ETH-EUR") == "ETH-EUR"


def test_parse_crypto_us_stock_not_affected():
    # 确保无分隔符的 4 字符美股代码不被误判为加密货币
    p = parse("MSFT")
    assert p.market is Market.US
    assert p.currency == "USD"


def test_parse_crypto_unknown_base_with_hyphen():
    # 连字符格式: 只要后缀是法币就识别为 crypto (宽松匹配)
    p = parse("FOO-USD")
    assert p.market is Market.CRYPTO
    assert p.currency == "USD"


def test_parse_crypto_stablecoin_quote():
    # 稳定币计价: BTC-USDT 也识别为 crypto, 币种保留 USDT
    p = parse("BTC-USDT")
    assert p.market is Market.CRYPTO
    assert p.currency == "USDT"
    assert p.yahoo == "BTC-USDT"


def test_normalize_crypto_stablecoin_no_separator():
    assert normalize("BTCUSDT") == "BTC-USDT"
    assert normalize("ethusdt") == "ETH-USDT"


def test_parse_crypto_suffix_quote_lower_input():
    # 大小写不敏感 + 首尾空白
    p = parse("  btc-usd ")
    assert p.yahoo == "BTC-USD"
    assert p.market is Market.CRYPTO


def test_parse_crypto_non_crypto_hyphen_is_error():
    # 连字符后缀不是计价货币时不能误判为加密货币
    with pytest.raises(ValueError):
        parse("FOO-BAR")


def test_parse_crypto_short_base_not_crypto():
    # len<=3 的无分隔符代码 (如 700) 不走 crypto 补全, 仍按无后缀处理
    assert normalize("700") == "700"
    with pytest.raises(ValueError):
        parse("700.ZZ")


def test_parse_crypto_4char_us_stock_boundary():
    # 4 字符无分隔符且不在已知 crypto 表内 → 美股 (不误判)
    p = parse("COIN")
    assert p.market is Market.US
    # 已知 crypto 基础代码 + 法币后缀才会补全
    assert normalize("SOLUSD") == "SOL-USD"


# ---------- 权威 type 字段 (系统内部域标记) ----------


def test_type_for_symbol_three_domains():
    from tracker.symbols import type_for_symbol

    assert type_for_symbol("AAPL") == "global"
    assert type_for_symbol("0700.HK") == "global"
    assert type_for_symbol("SAP.DE") == "global"
    assert type_for_symbol("600519.SS") == "cn"
    assert type_for_symbol("200012.SZ") == "cn"
    assert type_for_symbol("830799.BJ") == "cn"
    assert type_for_symbol("BTC-USD") == "crypto"
    assert type_for_symbol("ETH-USDT") == "crypto"


def test_parse_sets_type():
    assert parse("AAPL").type == "global"
    assert parse("600519.SS").type == "cn"
    assert parse("900902.SS").type == "cn"  # B股仍在 cn 域
    assert parse("BTC-USD").type == "crypto"


def test_type_same_suffix_never_two_domains():
    from tracker.symbols import type_for_symbol

    # 互斥性: 连字符+计价货币永远 crypto, 点后缀/裸代码永远股票
    assert type_for_symbol("BRK-B") == "global"  # 美股类别股不是 crypto
    assert parse("BRK-B").type == "global"
