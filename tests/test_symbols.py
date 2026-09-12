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
