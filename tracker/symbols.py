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

PENCE_CURRENCIES = {"GBp", "GBX", "GBx"}

# 加密货币常见基础代码 (用于 BTCUSD → BTC-USD 自动补全)
_KNOWN_CRYPTO: set[str] = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC", "AVAX",
    "LINK", "LTC", "BCH", "UNI", "ATOM", "XLM", "NEAR", "TRX", "ICP", "FIL",
    "HBAR", "VET", "ALGO", "AAVE", "MKR", "SNX", "COMP", "GRT", "LDO", "OP",
    "ARB", "APT", "INJ", "SUI", "SEI", "TIA", "STX", "RUNE", "PEPE", "WIF",
    "BONK", "JUP", "PYTH", "DYDX", "ORDI", "TON", "SHIB", "ETC", "XMR", "ZEC",
    "FTM", "SAND", "MANA", "AXS", "IMX", "GALA", "CRV", "SUSHI", "1INCH",
}

# 加密货币计价货币 (法币 + 主流稳定币; Yahoo Finance 用 BTC-USD 格式)
_CRYPTO_QUOTES: set[str] = {
    "USD", "EUR", "GBP", "JPY", "KRW", "AUD", "CAD", "CHF", "SGD", "HKD",
    "INR", "BRL", "CNY", "RUB", "TRY", "MXN", "ZAR", "THB", "IDR",
    "USDT", "USDC", "DAI", "BUSD",
}


@dataclass(frozen=True)
class ParsedSymbol:
    raw: str
    yahoo: str
    market: Market
    currency: str
    is_b_share: bool = False

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
    # 加密货币无分隔符格式: BTCUSD → BTC-USD, BTCUSDT → BTC-USDT (Yahoo Finance 格式)
    if "-" not in s and "." not in s and len(s) > 3:
        # 计价货币按长度从长到短匹配 (USDT/USDC/BUSD 4位, 其余 3位)
        for qlen in (4, 3):
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


def parse(symbol: str) -> ParsedSymbol:
    yahoo = normalize(symbol)
    # 加密货币: BTC-USD / ETH-EUR 等连字符格式 (Yahoo Finance crypto 行情)
    if "-" in yahoo and "." not in yahoo:
        base, _, quote = yahoo.rpartition("-")
        if quote in _CRYPTO_QUOTES:
            return ParsedSymbol(
                raw=symbol.strip(),
                yahoo=yahoo,
                market=Market.CRYPTO,
                currency=quote,
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
        if code.startswith(("900", "200")):
            is_b_share = True
    return ParsedSymbol(
        raw=symbol.strip(), yahoo=yahoo, market=market, currency=currency, is_b_share=is_b_share
    )
