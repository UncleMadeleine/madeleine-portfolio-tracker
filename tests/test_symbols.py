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
