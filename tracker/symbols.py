"""Symbol 解析与市场识别 (Yahoo Finance 后缀规范)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    US = "US"
    CN = "CN"
    HK = "HK"
    DE = "DE"
    GB = "GB"
    CA = "CA"
    AU = "AU"
    SG = "SG"
    BR = "BR"
    JP = "JP"
    KR = "KR"
    BJ = "BJ"
    CRYPTO = "CRYPTO"
    INDEX = "INDEX"


MARKET_META: dict[Market, dict[str, str]] = {
    Market.US: {"label": "美股", "currency": "USD"},
    Market.CN: {"label": "A股", "currency": "CNY"},
    Market.HK: {"label": "港股", "currency": "HKD"},
    Market.DE: {"label": "德股", "currency": "EUR"},
    Market.GB: {"label": "英股", "currency": "GBP"},
    Market.CA: {"label": "加股", "currency": "CAD"},
    Market.AU: {"label": "澳股", "currency": "AUD"},
    Market.SG: {"label": "新加坡股", "currency": "SGD"},
    Market.BR: {"label": "巴西股", "currency": "BRL"},
    Market.JP: {"label": "日股", "currency": "JPY"},
    Market.KR: {"label": "韩股", "currency": "KRW"},
    Market.BJ: {"label": "北交所", "currency": "CNY"},
    Market.CRYPTO: {"label": "加密货币", "currency": "USD"},
    Market.INDEX: {"label": "指数", "currency": "USD"},
}

_SUFFIX_MARKET: dict[str, Market] = {
    "SS": Market.CN,
    "SH": Market.CN,
    "SZ": Market.CN,
    "BJ": Market.BJ,
    "HK": Market.HK,
    "DE": Market.DE,
    "F": Market.DE,
    "BE": Market.DE,
    "DU": Market.DE,
    "HM": Market.DE,
    "MU": Market.DE,
    "L": Market.GB,
    "IL": Market.GB,
    "AL": Market.GB,
    "TO": Market.CA,
    "V": Market.CA,
    "CN": Market.CA,
    "NE": Market.CA,
    "AX": Market.AU,
    # 巴西 (B3): Yahoo 规范后缀 .SA (PETR4.SA / MBRF3.SA)
    "SA": Market.BR,
    # 新加坡 (SGX): Yahoo 规范后缀 .SI (D05.SI); .SG 为常见别名, normalize 归一
    "SI": Market.SG,
    "SG": Market.SG,
    # 日本 (东京证券交易所 JPX): Yahoo 规范后缀 .T (9984.T 软银 / 6758.T 索尼);
    # .JP 为常见别名, normalize 归一。Yahoo 上日本代码为 4 位数字, 不补零
    "T": Market.JP,
    "JP": Market.JP,
    # 韩国 (KRX 韩国交易所): Yahoo 规范后缀 .KS=KOSPI 主板 (005930.KS 三星电子
    # 普通股 / 005935.KS 优先股), .KQ=KOSDAQ (035720.KQ Celltrion)。
    # 6 位代码保留前导零; .KR 别名不收 (KOSPI/KOSDAQ 两板同后缀族, 无法判归哪板)
    "KS": Market.KR,
    "KQ": Market.KR,
}

# 宏观/风险指数目录: 与股票域完全隔离的独立代码规范 (IX.<KEY>).
# 分组是唯一数据源: 新增指数必须归入 INDEX_GROUPS 某组, 平铺目录 INDEX_CATALOG 由此派生
# (单一定义点, CLI --list 与 UI 下拉均从 index_groups() 读取, 不会再出现"目录有、分组无").
# yf: Yahoo Finance 全球指数代码; ak: akshare 新浪指数代码 (stock_zh_index_daily /
# index_us_stock_sina, 缺省 = key 本身); hsi: 恒生官网内部指数代码 (00001.00 等).
# currency 仅作展示 (指数无交割货币).
INDEX_GROUPS: dict[str, dict[str, dict[str, str]]] = {
    "风险/波动": {
        "VIX": {"name": "恐慌指数 VIX", "yf": "^VIX", "currency": "USD"},
        "VIX3M": {"name": "VIX 3个月", "yf": "^VIX3M", "currency": "USD"},
        "MOVE": {"name": "美债波动率 MOVE", "yf": "^MOVE", "currency": "USD"},
    },
    "美元/利率": {
        "DXY": {"name": "美元指数", "yf": "DX-Y.NYB", "currency": "USD"},
        "US10Y": {"name": "美债 10 年收益率", "yf": "^TNX", "currency": "%"},
        # ^IRX 是 Cboe 13 周国库券收益率 (Yahoo 无 2 年期); 原错标为"2 年",
        # 按数据实际口径正名为 3 个月, 避免把 3 个月收益率当 2 年读
        "US3M": {"name": "美债 3 个月收益率", "yf": "^IRX", "currency": "%"},
    },
    "美股": {
        "SPX": {"name": "标普 500", "yf": "^GSPC", "currency": "点"},
        "NDX": {"name": "纳斯达克 100", "yf": "^NDX", "currency": "点"},
        "DJI": {"name": "道琼斯工业", "yf": "^DJI", "currency": "点"},
        "RUT": {"name": "罗素 2000", "yf": "^RUT", "currency": "点"},
    },
    "全球": {
        "DAX": {"name": "德国 DAX", "yf": "^GDAXI", "currency": "点"},
        "FTSE": {"name": "英国富时 100", "yf": "^FTSE", "currency": "点"},
        "N225": {"name": "日经 225", "yf": "^N225", "currency": "点"},
    },
    # 恒生指数公司官网 (hsi.com.hk, 见 providers/index.py): 官网公开 API 零鉴权,
    # 只发布日收盘 (滚动约 5 年窗口), 无开高低 → 平K线。hsi 字段 = 恒生内部
    # 指数代码。Yahoo 对港股指数仅 ^HSI/^HSCE 有数据, 其余官网独有, 且官网
    # 收盘口径与 Yahoo 开高低口径混用会画出两种K线, 故全组专属源无兜底。
    "港股": {
        "HSI": {"name": "恒生指数", "hsi": "00001.00", "currency": "点"},
        "HSCEI": {
            "name": "恒生中国企业指数 (国企)",
            "hsi": "00014.00",
            "currency": "点",
        },
        "HSTECH": {"name": "恒生科技指数", "hsi": "02083.00", "currency": "点"},
        "HSCCI": {
            "name": "恒生香港中资企业指数 (红筹)",
            "hsi": "00015.00",
            "currency": "点",
        },
        "HSCI": {"name": "恒生综合指数", "hsi": "00011.00", "currency": "点"},
        "HSHK35": {"name": "恒生香港35", "hsi": "00020.00", "currency": "点"},
        "HSHKI": {"name": "恒生港股通指数", "hsi": "02019.00", "currency": "点"},
        "VHSI": {"name": "恒指波幅指数", "hsi": "01050.00", "currency": "点"},
        "HSIII": {
            "name": "恒生互联网科技业指数",
            "hsi": "02016.00",
            "currency": "点",
        },
        "HSHCI": {"name": "恒生医疗保健指数", "hsi": "02017.00", "currency": "点"},
        "HSCGSI": {"name": "恒生消费指数", "hsi": "02018.00", "currency": "点"},
        "HSBIO": {"name": "恒生生物科技指数", "hsi": "02077.00", "currency": "点"},
    },
    "中国": {
        # ak = 新浪代码 (akshare 优先); yf = Yahoo 兜底 (akshare 失败时不再 KeyError)
        "CSI300": {
            "name": "沪深 300",
            "ak": "sh000300",
            "yf": "000300.SS",
            "currency": "点",
        },
        "CSI500": {
            "name": "中证 500",
            "ak": "sh000905",
            "yf": "000905.SS",
            "currency": "点",
        },
        "CSI1000": {
            "name": "中证 1000",
            "ak": "sh000852",
            "yf": "000852.SS",
            "currency": "点",
        },
        "SSE": {
            "name": "上证指数",
            "ak": "sh000001",
            "yf": "000001.SS",
            "currency": "点",
        },
        "SZSE": {
            "name": "深证成指",
            "ak": "sz399001",
            "yf": "399001.SZ",
            "currency": "点",
        },
        "CYB": {
            "name": "创业板指",
            "ak": "sz399006",
            "yf": "399006.SZ",
            "currency": "点",
        },
        "KECHUANG50": {
            "name": "科创 50",
            "ak": "sh000688",
            "yf": "000688.SS",
            "currency": "点",
        },
        # 恒生沪深港通AH股溢价指数 (恒生指数公司, 彭博 HSAHP / 路孚特 .HSCAHPI):
        # 100 = A/H 同价, >100 = A股溢价, 市值加权, 约 149 只 AH 双重上市公司。
        # hsi 字段 = 恒生内部指数代码 (seriesCode=ahpremium), 走官网公开 API
        # (零鉴权, 见 providers/index.py); 该指数不发布开高低, 为平K线。
        "HSAHP": {
            "name": "AH股溢价指数",
            "hsi": "01044.00",
            "currency": "点",
        },
    },
    # 商品期货 (新浪主力连续, akshare futures_main_sina; 见 providers/index.py)
    # futures 字段 = 新浪主连代码 (<品种><0>), 已收录上期所/大商所/广期所主力连续。
    # 主力连续按新浪规则拼接近月合约, 换月处有跳空 (属正常构造); currency 为报价单位。
    "商品/贵金属": {
        # 上期所黄金主连 AU0: 元/克
        "AU": {"name": "黄金主力连续", "futures": "AU0", "currency": "元/克"},
        # 上期所白银主连 AG0: 元/千克
        "AG": {"name": "白银主力连续", "futures": "AG0", "currency": "元/千克"},
    },
    "商品/农产品": {
        # 大商所鸡蛋主连 JD0: 元/500千克
        "JD": {"name": "鸡蛋主力连续", "futures": "JD0", "currency": "元/500千克"},
        # 大商所豆粕主连 M0: 元/吨
        "M": {"name": "豆粕主力连续", "futures": "M0", "currency": "元/吨"},
        # 大商所黄大豆1号/2号主连 (A0 非转基因食用豆 / B0 进口转基因压榨豆): 元/吨
        "A": {"name": "豆一主力连续", "futures": "A0", "currency": "元/吨"},
        "B": {"name": "豆二主力连续", "futures": "B0", "currency": "元/吨"},
        # 大商所生猪主连 LH0: 元/吨
        "LH": {"name": "生猪主力连续", "futures": "LH0", "currency": "元/吨"},
    },
    # 广期所碳酸锂期货主力连续 (2023-07-21 上市), 单位 元/吨
    "商品/新能源": {
        "LC": {"name": "碳酸锂主力连续", "futures": "LC0", "currency": "元/吨"},
    },
    # Cameco 官网铀价 (见 providers/index.py): 月末 UxC + TradeTech 行业均价,
    # 月频且不发布开高低 → 平K线。cameco 字段选择序列: spot=现货 / lt=长协。
    "商品/能源": {
        "URANIUM": {
            "name": "铀现货价 U3O8",
            "cameco": "spot",
            "currency": "美元/磅",
        },
        "URANIUM_LT": {
            "name": "铀长协价 U3O8",
            "cameco": "lt",
            "currency": "美元/磅",
        },
    },
    # 中国水泥网 (index.ccement.com, 免登录前端接口; 见 providers/index.py)
    "水泥网": {
        "CEMPI": {
            "name": "水泥价格指数 CEMPI",
            "ccement": "kline",
            "currency": "点",
        },
        "CEMPIPO": {
            "name": "P.O 42.5 水泥价格指数",
            "ccement": "priceindex/po425zsline",
            "currency": "元/吨",
        },
        "CCPDI": {"name": "水泥煤价差指数 CCPDI", "ccement": "coal", "currency": "点"},
        "CLINKER": {
            "name": "熟料价格指数",
            "ccement": "clinker/ClinkerPrice",
            "currency": "元/吨",
        },
        "CONCRETE": {
            "name": "混凝土价格指数",
            "ccement": "concrete/ConcretePrice",
            "currency": "元/方",
        },
        "CSPI": {
            "name": "碎石价格指数 CSPI",
            "ccement": "stone/stoneZSLine",
            "currency": "元/吨",
        },
        "MSPI": {
            "name": "机制砂价格指数 MSPI",
            "ccement": "manufactured/manufacturedZSLine",
            "currency": "元/吨",
        },
        "MORPI": {
            "name": "预拌砂浆价格指数 MORPI",
            "ccement": "mortar/mortarZSLine",
            "currency": "元/吨",
        },
    },
}

# 平铺目录 (派生): {key: 条目}, 保分组定义顺序
INDEX_CATALOG: dict[str, dict[str, str]] = {
    key: dict(entry)
    for entries in INDEX_GROUPS.values()
    for key, entry in entries.items()
}


def index_groups() -> dict[str, list[str]]:
    """指数分组视图: {组名: [key, ...]}, 供 CLI --list 与 UI 下拉共用."""
    return {group: list(entries) for group, entries in INDEX_GROUPS.items()}


def index_key(symbol: str) -> str | None:
    """IX.<KEY> → 指数 key; 非指数代码返回 None (供 type 推导提前短路)."""
    s = str(symbol).strip().upper()
    if not s.startswith("IX.") or len(s) <= 3:
        return None
    key = s[3:]
    return key if key in INDEX_CATALOG else None


def index_label(key: str) -> str:
    """指数 key → 展示名 (如 DXY → 美元指数); 未知 key 原样返回."""
    e = INDEX_CATALOG.get(key)
    return e["name"] if e else key


# 便士计价符号: LSE 以 GBp/GBX 报价 (1 GBP = 100 便士), 实际数据源大小写混用。
# 判定规则: 代码形如 <G><B><p|X> (任意大小写) 即便士; 精确的 "GBP" 是英镑本体,
# 若把它当便士会在 GBP 报价上再除 100, 结果偏小 100 倍。


def is_pence(currency: str) -> bool:
    """该币种代码是否为 LSE 便士计价 (GBp/GBX 任意大小写; 精确 "GBP" 不算)."""
    s = str(currency).strip()
    # "GBP" (精确大写) 是英镑本体, 不能再除 100; "gbp" 等小写变体按便士处理
    # (数据源从不用小写表示英镑本体, 大小写混用只出现在便士符号上)
    if not s or s == "GBP":
        return False
    return s.upper() in ("GBP", "GBX")


# 加密货币常见基础代码 (用于 BTCUSD → BTC-USD 自动补全)
_KNOWN_CRYPTO: set[str] = {
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "ADA",
    "DOGE",
    "DOT",
    "MATIC",
    "AVAX",
    "LINK",
    "LTC",
    "BCH",
    "UNI",
    "ATOM",
    "XLM",
    "NEAR",
    "TRX",
    "ICP",
    "FIL",
    "HBAR",
    "VET",
    "ALGO",
    "AAVE",
    "MKR",
    "SNX",
    "COMP",
    "GRT",
    "LDO",
    "OP",
    "ARB",
    "APT",
    "INJ",
    "SUI",
    "SEI",
    "TIA",
    "STX",
    "RUNE",
    "PEPE",
    "WIF",
    "BONK",
    "JUP",
    "PYTH",
    "DYDX",
    "ORDI",
    "TON",
    "SHIB",
    "ETC",
    "XMR",
    "ZEC",
    "FTM",
    "SAND",
    "MANA",
    "AXS",
    "IMX",
    "GALA",
    "CRV",
    "SUSHI",
    "1INCH",
}

# 加密货币计价货币 (法币 + 主流稳定币 + 主流币本位; Yahoo Finance 用 BTC-USD 格式)
# 必须覆盖 search._CRYPTO_QUOTE_ASSETS: 目录产出的每个 BASE-QUOTE 都要能被 parse 识别
_CRYPTO_QUOTES: set[str] = {
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "KRW",
    "AUD",
    "CAD",
    "CHF",
    "SGD",
    "HKD",
    "INR",
    "BRL",
    "CNY",
    "RUB",
    "TRY",
    "MXN",
    "ZAR",
    "THB",
    "IDR",
    "USDT",
    "USDC",
    "DAI",
    "BUSD",
    "FDUSD",
    "TUSD",
    "BTC",
    "ETH",
    "BNB",
}


STABLE_USD_QUOTES: frozenset[str] = frozenset(
    {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
)

#: 币本位计价 (既非法币也非 USD 等价稳定币): 无法折算, 视图层给明确原因
COIN_QUOTES: frozenset[str] = frozenset({"BTC", "ETH", "BNB"})


def valuation_currency(ccy: str) -> str:
    """计价货币 -> 估值货币: USD 等价稳定币归一为 USD, 其余原样 (大小写不敏感).

    单一权威点: symbols.parse / providers.crypto 的行情币种 / fx 的汇率查表
    都走这里, 保证「代码 -> 行情 -> 估值」全链路口径一致。
    """
    c = str(ccy or "").strip().upper()
    return "USD" if c in STABLE_USD_QUOTES else c


def _is_us_preferred(quote: str) -> bool:
    """美股优先股系列后缀: Yahoo 惯例 BASE-P<系列字母> (WFC-PL / BAC-PL / GS-PA).

    系列 1 个字母 (Yahoo 实际全部单系列字母); 两位以上会放过垃圾代码 (XX-PP)。
    """
    return (
        len(quote) == 2
        and quote[0] == "P"
        and quote[1].isalpha()
        and quote[1].isupper()
    )


@dataclass(frozen=True)
class ParsedSymbol:
    raw: str
    yahoo: str
    market: Market
    currency: str
    is_b_share: bool = False
    type: str = "global"  # 权威域标记: global / cn / crypto (内部字段, 用户不可见)

    @property
    def market_label(self) -> str:
        if self.is_b_share and self.market is Market.CN:
            return "B股"
        return MARKET_META[self.market]["label"]

    @property
    def ak_code(self) -> str | None:
        if self.market in (Market.CN, Market.BJ):
            return self.yahoo.split(".")[0]
        if self.market is Market.HK:
            return self.yahoo.split(".")[0].lstrip("0").zfill(5)
        return None


def normalize(symbol: str) -> str:
    # 容忍 None / 数字等非字符串输入 (手改 JSON 时 symbol 可能写成 700):
    # 归一函数本身保持 total, 由 parse() 统一做合法性校验。
    s = ("" if symbol is None else str(symbol)).strip().upper()
    # 加密货币无分隔符格式: BTCUSD → BTC-USD, BTCFDUSD → BTC-FDUSD (Yahoo Finance 格式)
    if "-" not in s and "." not in s and len(s) > 3:
        # 计价货币按长度从长到短匹配 (FDUSD 5位, USDT/USDC/BUSD 4位, 其余 3位)
        for qlen in sorted({len(q) for q in _CRYPTO_QUOTES}, reverse=True):
            if len(s) <= qlen:
                continue
            quote = s[-qlen:]
            base = s[:-qlen]
            if quote in _CRYPTO_QUOTES and base in _KNOWN_CRYPTO:
                return f"{base}-{quote}"
    if "." not in s:
        return s
    head, _, suffix = s.rpartition(".")
    if suffix == "SH":
        suffix = "SS"
    elif suffix == "HK":
        head = head.lstrip("0").zfill(4)
    elif suffix == "SG":
        suffix = "SI"  # SGX: Yahoo 规范后缀 .SI; .SG 别名归一 (yfinance 无 .SG 数据)
    elif suffix == "JP":
        suffix = "T"  # 东京: Yahoo 规范后缀 .T; .JP 别名归一
    elif suffix in ("KS", "KQ"):
        # 韩股 Yahoo 代码是 6 位带前导零 (005930.KS): 与港股补零同口径,
        # 否则 5930.KS 这类输入 parse 合法但 yfinance/akshare 取不到数,
        # 只报「行情缺失」而不提示代码形态不对
        if head.isdigit():
            head = head.lstrip("0").zfill(6)
    return f"{head}.{suffix}"


def type_for_symbol(yahoo: str) -> str:
    """自定义后缀规范 → 权威域标记 (唯一推导规则, 供配置打标与导入使用).

    global: 裸代码 (美股, 含 BRK-B 类别股 / WFC-PL 优先股) 与 .HK/.DE/.L/.TO/.AX
            等全球股票后缀
    cn:     .SS/.SZ (沪深 A/B 股) 与 .BJ (北交所)
    crypto: BASE-QUOTE 连字符格式 (计价货币为法币/稳定币/主流币本位)
    index:  IX.<KEY> 宏观/风险指数 (IX.DXY 美元指数 / IX.VIX 恐慌指数 ...)

    与 parse() 的判定语义完全一致: 单字母连字符 (BRK-B) 是美股类别代码,
    优先股系列 (WFC-PL) 是美股优先股 —— 都不是加密货币, 保证同一代码在任何
    路径下都不会路由到两个域。
    先经 normalize() 归一 (如 600519.SH → 600519.SS, 00700.HK → 0700.HK,
    BTCUSDT → BTC-USDT), 否则配置里手写的别名会被打上错误的域标记。
    """
    s = normalize(yahoo)
    if "-" in s and "." not in s:
        base, _, quote = s.rpartition("-")
        is_us_class = len(quote) == 1 and quote.isalpha()
        # parse() 对无法识别的连字符代码显式报错; 打标只需处理三类合法形态:
        # crypto (BASE-QUOTE, 计价货币为法币/稳定币), 美股类别股 (单字母后缀),
        # 美股优先股系列 (WFC-PL)
        if (
            not is_us_class
            and not _is_us_preferred(quote)
            and (base in _KNOWN_CRYPTO or quote in _CRYPTO_QUOTES)
        ):
            return "crypto"
    # 宏观/风险指数 (IX.<KEY>): 独立域, 与股票代码永不冲突 (IX 前缀 + 目录校验)
    if index_key(s):
        return "index"
    if "." in s:
        suffix = s.rsplit(".", 1)[1]
        if suffix in ("SS", "SZ", "BJ"):
            return "cn"
    return "global"


def parse(symbol: str) -> ParsedSymbol:
    # 非字符串 / 空代码显式报错: 手改 JSON 写成 "symbol": 700 时不再抛
    # AttributeError('int' object has no attribute 'strip') 穿透成裸 traceback,
    # 也避免空串被静默当成合法美股 (yahoo="") 去请求数据源。
    if not isinstance(symbol, str):
        raise ValueError(f"代码必须是字符串 (得到 {type(symbol).__name__}: {symbol!r})")
    if not symbol.strip():
        raise ValueError("代码不能为空")
    # 宏观/风险指数: IX.<KEY> (独立域, 目录校验; 与股票代码永不冲突)
    yahoo = normalize(symbol)
    ikey = index_key(yahoo)
    if ikey:
        return ParsedSymbol(
            raw=symbol.strip(),
            yahoo=yahoo,
            market=Market.INDEX,
            currency=INDEX_CATALOG[ikey]["currency"],
            type="index",
        )

    # 加密货币: BTC-USD / ETH-EUR 等连字符格式 (Yahoo Finance crypto 行情)
    if yahoo.startswith("IX."):
        raise ValueError(
            f"未收录的指数代码: {symbol} (指数目录: {', '.join(INDEX_CATALOG)})"
        )
    if "-" in yahoo and "." not in yahoo:
        base, _, quote = yahoo.rpartition("-")
        if quote in _CRYPTO_QUOTES:
            return ParsedSymbol(
                raw=symbol.strip(),
                yahoo=yahoo,
                market=Market.CRYPTO,
                # 稳定币计价按 USD 等价估值 (BTC-USDT 的币种口径即 USD):
                # 否则汇率链没有 USDT 通道, 持仓整行被排除出视图
                currency=valuation_currency(quote),
                type="crypto",
            )
    if "-" in yahoo and "." not in yahoo:
        base, _, quote = yahoo.rpartition("-")
        # 连字符既不是 crypto 计价货币、也不符合美股类别代码 (如 BRK-B / BRK.B)
        # 或美股优先股系列 (WFC-PL) 的形态 (类别 1 个大写字母; 优先股 P+系列字母)
        # 且 base 非已知 crypto → 大概率是无效代码, 显式报错
        if (
            base not in _KNOWN_CRYPTO
            and not (len(quote) == 1 and quote.isalpha())
            and not _is_us_preferred(quote)
        ):
            raise ValueError(f"无法识别的代码: {symbol}")
    if "." not in yahoo:
        market = Market.US
    else:
        suffix = yahoo.rsplit(".", 1)[1]
        market = _SUFFIX_MARKET.get(suffix)
        if market is None:
            raise ValueError(f"无法识别的交易所后缀: {symbol}")
    currency = MARKET_META[market]["currency"]
    is_b_share = False
    if market is Market.CN:
        code = yahoo.split(".")[0]
        if code.startswith("900"):
            is_b_share = True
            currency = "USD"  # 沪 B 以美元交易
        elif code.startswith("200"):
            is_b_share = True
            currency = "HKD"  # 深 B 以港币交易
    elif market is Market.HK:
        # 港股人民币柜台 (HKEX RMB counter): 8 开头 5 位码 (82318.HK 中国平安-R /
        # 80737.HK 湾区发展-R / 83188.HK 华夏沪深三百-R), 以 CNY 成交。号段专用,
        # 必须同时要求 5 位: GEM 的 08083 (有赞) 剥零后是 4 位 8083.HK, 不是柜台。
        # 与 B 股同口径按代码形态推导, 供数据源不上报币种时兜底 (global_stocks
        # ._quote_from_dump / _akshare_quote 的 p.currency); 无需像 B 股那样覆盖
        # 数据源自报 —— yfinance 对人民币柜台报的就是 CNY (83188.HK)。
        # 美元柜台 (09xxx/41xxx, 仅 ETF 三柜台) 无法按形态推导: normalize 剥前导零
        # 后 9070.HK 与 9988.HK 阿里同形, 币种由数据源自报 (见 longport._currency_for)。
        code = yahoo.split(".")[0]
        if len(code) == 5 and code.startswith("8"):
            currency = "CNY"
    return ParsedSymbol(
        raw=symbol.strip(),
        yahoo=yahoo,
        market=market,
        currency=currency,
        is_b_share=is_b_share,
        type=type_for_symbol(yahoo),
    )
