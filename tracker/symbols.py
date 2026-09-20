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
    "SG": Market.DE,
    "MU": Market.DE,
    "L": Market.GB,
    "IL": Market.GB,
    "AL": Market.GB,
    "TO": Market.CA,
    "V": Market.CA,
    "CN": Market.CA,
    "NE": Market.CA,
    "AX": Market.AU,
}

# 宏观/风险指数目录: 与股票域完全隔离的独立代码规范 (IX.<KEY>).
# yf: Yahoo Finance 全球指数代码; ak: akshare 新浪指数代码 (stock_zh_index_daily /
# index_us_stock_sina, 缺省 = key 本身). currency 仅作展示 (指数无交割货币).
INDEX_CATALOG: dict[str, dict[str, str]] = {
    # -- 风险/波动率 --
    "VIX":  {"name": "恐慌指数 VIX", "yf": "^VIX", "currency": "USD"},
    "VIX3M": {"name": "VIX 3个月", "yf": "^VIX3M", "currency": "USD"},
    "MOVE": {"name": "美债波动率 MOVE", "yf": "^MOVE", "currency": "USD"},
    # -- 美元与利率 --
    "DXY":  {"name": "美元指数", "yf": "DX-Y.NYB", "currency": "USD"},
    "US10Y": {"name": "美债 10 年收益率", "yf": "^TNX", "currency": "%"},
    "US02Y": {"name": "美债 2 年收益率", "yf": "^IRX", "currency": "%"},
    # -- 美股指数 --
    "SPX":  {"name": "标普 500", "yf": "^GSPC", "currency": "点"},
    "NDX":  {"name": "纳斯达克 100", "yf": "^NDX", "currency": "点"},
    "DJI":  {"name": "道琼斯工业", "yf": "^DJI", "currency": "点"},
    "RUT":  {"name": "罗素 2000", "yf": "^RUT", "currency": "点"},
    # -- 全球指数 --
    "DAX":  {"name": "德国 DAX", "yf": "^GDAXI", "currency": "点"},
    "FTSE": {"name": "英国富时 100", "yf": "^FTSE", "currency": "点"},
    "N225": {"name": "日经 225", "yf": "^N225", "currency": "点"},
    "HSI":  {"name": "恒生指数", "yf": "^HSI", "currency": "点"},
    # -- 中国指数 (akshare 新浪源, 与 A 股域数据源惯例一致) --
    "CSI300": {"name": "沪深 300", "ak": "sh000300", "currency": "点"},
    "CSI500": {"name": "中证 500", "ak": "sh000905", "currency": "点"},
    "CSI1000": {"name": "中证 1000", "ak": "sh000852", "currency": "点"},
    "SSE":   {"name": "上证指数", "ak": "sh000001", "currency": "点"},
    "SZSE":  {"name": "深证成指", "ak": "sz399001", "currency": "点"},
    "CYB":   {"name": "创业板指", "ak": "sz399006", "currency": "点"},
    "KECHUANG50": {"name": "科创 50", "ak": "sh000688", "currency": "点"},
}

def index_key(symbol: str) -> str | None:
    """IX.<KEY> → 指数 key; 非指数代码返回 None (供 type 推导提前短路)."""
    s = str(symbol).strip().upper()
    if not s.startswith("IX.") or "." not in s[3:]:
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
def index_key(symbol: str) -> str | None:
    """IX.<KEY> → 指数 key; 非指数代码返回 None (供 type 推导提前短路)."""
    s = str(symbol).strip().upper()
    if not s.startswith("IX.") or len(s) <= 3:
        return None
    key = s[3:]
    return key if key in INDEX_CATALOG else None


def is_pence(currency: str) -> bool:
    """该币种代码是否为 LSE 便士计价 (GBp/GBX 任意大小写; 精确 "GBP" 不算)."""
    s = str(currency).strip()
    if s == "GBP":
        return False
    return s.upper() in ("GBP", "GBX")


# 加密货币常见基础代码 (用于 BTCUSD → BTC-USD 自动补全)
_KNOWN_CRYPTO: set[str] = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC", "AVAX",
    "LINK", "LTC", "BCH", "UNI", "ATOM", "XLM", "NEAR", "TRX", "ICP", "FIL",
    "HBAR", "VET", "ALGO", "AAVE", "MKR", "SNX", "COMP", "GRT", "LDO", "OP",
    "ARB", "APT", "INJ", "SUI", "SEI", "TIA", "STX", "RUNE", "PEPE", "WIF",
    "BONK", "JUP", "PYTH", "DYDX", "ORDI", "TON", "SHIB", "ETC", "XMR", "ZEC",
    "FTM", "SAND", "MANA", "AXS", "IMX", "GALA", "CRV", "SUSHI", "1INCH",
}

# 加密货币计价货币 (法币 + 主流稳定币 + 主流币本位; Yahoo Finance 用 BTC-USD 格式)
# 必须覆盖 search._CRYPTO_QUOTE_ASSETS: 目录产出的每个 BASE-QUOTE 都要能被 parse 识别
_CRYPTO_QUOTES: set[str] = {
    "USD", "EUR", "GBP", "JPY", "KRW", "AUD", "CAD", "CHF", "SGD", "HKD",
    "INR", "BRL", "CNY", "RUB", "TRY", "MXN", "ZAR", "THB", "IDR",
    "USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD",
    "BTC", "ETH", "BNB",
}


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
    s = symbol.strip().upper()
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
    return f"{head}.{suffix}"


def type_for_symbol(yahoo: str) -> str:
    """自定义后缀规范 → 权威域标记 (唯一推导规则, 供配置打标与导入使用).

    global: 裸代码 (美股, 含 BRK-B 类别股) 与 .HK/.DE/.L/.TO/.AX 等全球股票后缀
    cn:     .SS/.SZ (沪深 A/B 股) 与 .BJ (北交所)
    crypto: BASE-QUOTE 连字符格式 (计价货币为法币/稳定币/主流币本位)
    index:  IX.<KEY> 宏观/风险指数 (IX.DXY 美元指数 / IX.VIX 恐慌指数 ...)

    与 parse() 的判定语义完全一致: 单字母连字符 (BRK-B) 是美股类别代码,
    不是加密货币 —— 保证同一代码在任何路径下都不会路由到两个域。
    先经 normalize() 归一 (如 600519.SH → 600519.SS, 00700.HK → 0700.HK,
    BTCUSDT → BTC-USDT), 否则配置里手写的别名会被打上错误的域标记。
    """
    s = normalize(yahoo)
    if "-" in s and "." not in s:
        base, _, quote = s.rpartition("-")
        is_us_class = len(quote) == 1 and quote.isalpha()
        # parse() 对无法识别的连字符代码显式报错; 打标只需处理两类合法形态:
        # crypto (BASE-QUOTE, 计价货币为法币/稳定币) 与美股类别股 (单字母后缀)
        if not is_us_class and (base in _KNOWN_CRYPTO or quote in _CRYPTO_QUOTES):
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
    # 宏观/风险指数: IX.<KEY> (独立域, 目录校验; 与股票代码永不冲突)
    yahoo = normalize(symbol)
    ikey = index_key(yahoo)
    if ikey:
        return ParsedSymbol(
            raw=symbol.strip(), yahoo=yahoo, market=Market.INDEX,
            currency=INDEX_CATALOG[ikey]["currency"], type="index",
        )

    # 加密货币: BTC-USD / ETH-EUR 等连字符格式 (Yahoo Finance crypto 行情)
    if yahoo.startswith("IX."):
        raise ValueError(f"未收录的指数代码: {symbol} (指数目录: {', '.join(INDEX_CATALOG)})")
    if "-" in yahoo and "." not in yahoo:
        base, _, quote = yahoo.rpartition("-")
        if quote in _CRYPTO_QUOTES:
            return ParsedSymbol(
                raw=symbol.strip(),
                yahoo=yahoo,
                market=Market.CRYPTO,
                currency=quote,
                type="crypto",
            )
    if "-" in yahoo and "." not in yahoo:
        base, _, quote = yahoo.rpartition("-")
        # 连字符既不是 crypto 计价货币、也不符合美股类别代码 (如 BRK-B / BRK.B) 的形态
        # (美股类别 1 个大写字母) 且 base 非已知 crypto → 大概率是无效代码, 显式报错
        if base not in _KNOWN_CRYPTO and not (len(quote) == 1 and quote.isalpha()):
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
    return ParsedSymbol(
        raw=symbol.strip(), yahoo=yahoo, market=market, currency=currency,
        is_b_share=is_b_share, type=type_for_symbol(yahoo),
    )
