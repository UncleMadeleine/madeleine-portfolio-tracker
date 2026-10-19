import pytest

from tracker.symbols import MARKET_META, Market, normalize, parse


def test_normalize_hk_4digit():
    assert normalize("700.hk") == "0700.HK"
    assert normalize("00700.HK") == "0700.HK"
    assert normalize("80737.HK") == "80737.HK"


def test_normalize_hk_counter_codes():
    """港股 5 位柜台码按 Yahoo 惯例剥前导零补 4 位 (Yahoo 上就是此形态).

    03188.HK → 3188.HK (Yahoo 3188.HK 港元行情), 09070.HK → 9070.HK (美元柜台),
    83188.HK / 82318.HK / 41555.HK ≥5 位原样。剥零后美元柜台 (9070.HK) 与港币
    主板代码 (9988.HK 阿里) 同形, 币种不可按代码形态推导 (见 longport._currency_for)。
    """
    assert normalize("03188.HK") == "3188.HK"
    assert normalize("03070.HK") == "3070.HK"
    assert normalize("09070.HK") == "9070.HK"
    assert normalize("09188.HK") == "9188.HK"
    assert normalize("09988.HK") == "9988.HK"
    assert normalize("82318.HK") == "82318.HK"
    assert normalize("83188.HK") == "83188.HK"
    assert normalize("41555.HK") == "41555.HK"


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
    # B 股以外币计价: 沪 B 为美元, 深 B 为港币 (与 IBKR 合约币种、
    # yfinance 行情 currency 字段一致); 用 CNY 会导致汇率折算错误
    sh_b = parse("900902.SS")
    assert sh_b.market is Market.CN
    assert sh_b.currency == "USD"
    assert sh_b.ak_code == "900902"
    assert sh_b.is_b_share is True
    assert sh_b.market_label == "B股"
    sz_b = parse("200012.SZ")
    assert sz_b.market is Market.CN
    assert sz_b.currency == "HKD"
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


def test_parse_hk_rmb_counter_currency():
    """港股人民币柜台 (8 开头 5 位) 以 CNY 成交, 按代码形态推导.

    82318.HK 中国平安-R / 80737.HK 湾区发展-R (全球首只双币双股) / 83188.HK
    华夏沪深三百-R。必须同时要求 5 位: GEM 的 08083 (有赞) 剥零后是 4 位
    8083.HK, 不属该号段。美元柜台 (09070.HK 平安香港高息-U) 剥零后与港币主板
    代码同形 (9070.HK vs 9988.HK 阿里), 无法按形态推导 → HKD, 币种由数据源自报
    (见 longport._currency_for)。
    """
    for sym in ("82318.HK", "80737.HK", "82800.HK", "83188.HK"):
        p = parse(sym)
        assert p.market is Market.HK
        assert p.currency == "CNY", sym
        assert p.is_b_share is False, sym
        assert p.ak_code == sym.split(".")[0], sym
    # 4 位 8 开头 (GEM 08083 有赞) 不是人民币柜台, 仍是港币
    p = parse("8083.HK")
    assert p.currency == "HKD"
    assert p.ak_code == "08083"
    # 港币主柜台 (含 9 开头四位) 一律 HKD
    assert parse("0700.HK").currency == "HKD"
    assert parse("9988.HK").currency == "HKD"  # 阿里
    # 美元柜台: 不按形态猜
    assert parse("09070.HK").currency == "HKD"
    assert parse("09188.HK").currency == "HKD"
    # 市场默认不变 (MARKET_META), 只是按号段覆盖
    assert MARKET_META[Market.HK]["currency"] == "HKD"


def test_parse_all_markets():
    assert parse("SAP.DE").market is Market.DE
    assert parse("SAP.F").market is Market.DE
    assert parse("BP.L").market is Market.GB
    assert parse("LSEG.L").market is Market.GB
    assert parse("BP.IL").market is Market.GB
    assert parse("RY.TO").market is Market.CA
    assert parse("X.V").market is Market.CA
    assert parse("BHP.AX").market is Market.AU
    assert parse("D05.SI").market is Market.SG


def test_parse_lse_main_market():
    """LSE 主板英股: LSEG.L 伦敦证券交易所集团。

    parse 的 currency 是本地规范口径 (GBP 英镑, avg_cost 按 £88.00 记);
    Yahoo 对 LSE 报价为便士 (GBp), ÷100 折算发生在 provider 层
    (global_stocks._quote_from_dump / ibkr.quote_from_ticker)。
    """
    p = parse("LSEG.L")
    assert p.yahoo == "LSEG.L"
    assert p.market is Market.GB
    assert p.currency == "GBP"
    assert p.market_label == "英股"
    assert p.type == "global"
    assert normalize("lseg.l") == "LSEG.L"


def test_parse_lse_iob_suffix():
    """.IL = LSE 国际挂盘 (IOB): KAP.IL Kazatomprom GDR (IOB/USD)。

    裸 KAP 是美股已退市代码 (KapStone), 必须带 .IL 后缀才取得到行情;
    parse 不归一掉该后缀 (与 .L 并存, 两者在 Yahoo 是不同交易所)。
    """
    p = parse("KAP.IL")
    assert p.yahoo == "KAP.IL"
    assert p.market is Market.GB
    assert p.type == "global"
    # normalize 保持原样 (不被改写成 .L 或剥成裸代码)
    assert normalize("KAP.IL") == "KAP.IL"


def test_parse_singapore_alias_and_normalization():
    """SGX: .SI 规范后缀 (SGD); .SG 别名归一为 .SI (yfinance 无 .SG 数据)。"""
    p = parse("D05.SI")
    assert p.yahoo == "D05.SI"
    assert p.market is Market.SG
    assert p.currency == "SGD"
    assert p.market_label == "新加坡股"
    assert p.type == "global"
    # 别名大小写/空白/小写后缀一律归一
    assert parse("D05.SG").yahoo == "D05.SI"
    assert parse("d05.sg").yahoo == "D05.SI"
    assert parse(" U11.SI ").yahoo == "U11.SI"
    from tracker.symbols import type_for_symbol

    assert type_for_symbol("D05.SG") == "global"
    # .SG 不再误判德股 (回归: 德股仅 .DE/.F/.BE/.DU/.HM/.MU)
    assert parse("SAP.SG").market is Market.SG
    assert parse("SAP.SG").currency == "SGD"


def test_parse_unknown_suffix():
    with pytest.raises(ValueError):
        parse("FOO.ZZ")


def test_parse_rejects_empty_and_non_string():
    """空代码 / 非字符串代码显式报可读错误, 不静默产出空 yahoo 美股.

    手改 JSON 把 symbol 写成数字 (700) 时, 旧实现抛 AttributeError 并穿透
    整个快照; 空串则被当成合法的美股空代码去请求数据源。
    """
    with pytest.raises(ValueError, match="代码必须是字符串"):
        parse(700)
    with pytest.raises(ValueError, match="代码必须是字符串"):
        parse(None)
    with pytest.raises(ValueError, match="代码不能为空"):
        parse("")
    with pytest.raises(ValueError, match="代码不能为空"):
        parse("   ")
    # normalize 保持 total (供 type_for_symbol 等内部路径兜底)
    assert normalize(None) == ""
    assert normalize(700) == "700"


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


def test_parse_crypto_alt_quote_assets():
    # 币本位计价 (search 目录会产出 BTC/ETH/BNB/FDUSD 计价的交易对):
    # 必须识别为 crypto 并保留计价货币, 不能落回美股/USD
    for sym, ccy in (
        ("ETH-BTC", "BTC"),
        ("ARKM-BTC", "BTC"),
        ("WIF-ETH", "ETH"),
        ("PEPE-BNB", "BNB"),
        ("BTC-FDUSD", "FDUSD"),
    ):
        p = parse(sym)
        assert p.market is Market.CRYPTO, sym
        assert p.currency == ccy, sym
        assert p.type == "crypto", sym
        assert p.market_label == "加密货币", sym


def test_normalize_crypto_alt_quote_no_separator():
    assert normalize("ETHBTC") == "ETH-BTC"
    assert normalize("BTCFDUSD") == "BTC-FDUSD"


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


def test_type_for_symbol_normalizes_aliases():
    from tracker.symbols import type_for_symbol

    # 手写别名必须先归一, 否则配置会被打上与 parse 不一致的域标记
    for sym in ("600519.SH", "00700.HK", "btc-usd", "BTCUSDT", "ETHBTC"):
        assert type_for_symbol(sym) == parse(sym).type, sym
    assert type_for_symbol("600519.SH") == "cn"
    assert type_for_symbol("BTCUSDT") == "crypto"


def test_is_pence_case_insensitive():
    from tracker.symbols import is_pence

    # LSE 便士符号各数据源大小写不一 (yfinance 用 GBp, IBKR 用 GBX/GBp)
    for ccy in ("GBp", "GBX", "GBx", "gbx", "gbX", "gBp", "  GBp "):
        assert is_pence(ccy), ccy
    # 精确 "GBP" 是英镑本体, 不能再除 100 (便士与英镑只差大小写, 不能放宽)
    assert not is_pence("GBP")
    assert not is_pence("USD")
    assert not is_pence("")
