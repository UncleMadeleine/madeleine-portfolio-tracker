"""akshare / 东财股票行情与历史源 (cn/global/index 域共用).

从 global_stocks 拆出: 东财单代码行情、A股/港股日 K 与缓存/单飞逻辑被
CN、全球港股兜底、orchestration 多处复用。集中在此避免跨域私有导入。
"""

from __future__ import annotations

import math
import threading
import time

import pandas as pd

from ..deps import akshare as _ak
from ..deps import requests as _requests
from ..symbols import Market, ParsedSymbol, is_currency_code
from ..util import with_timeout
from .base import Quote

# akshare 单代码日K (stock_zh_a_hist / stock_hk_hist): 接口不传 timeout,
# 给硬超时防挂死 (单代码请求通常 1-3s, 30s 覆盖慢网/重试)
_AK_HIST_TIMEOUT = 30.0
# 单代码实时行情失败占位 TTL: 东财不可达/限流时不反复打 (与全市场 spot 的
# 失败占位同思路, 现在是 per-symbol 粒度)
_AK_FAIL_TTL = 15.0

# ---------- 实时行情源: 东财 push2 单代码 (akshare 同源, per-symbol) ----------
#
# 为什么不用 akshare 的全市场 spot (stock_zh_a_spot_em / stock_hk_spot_em):
#   那两个接口内部 fetch_paginated_data 分页拉全市场 (A股 ~5926 只 = 60 页,
#   每页之间 sleep 0.5-1.5s), 单次实测 42-102s —— 全市场 spot 任何秒级
#   超时都必然失败, 「A股默认走 akshare / 港股 akshare 兜底」实际从未生效。
#   单代码 stock/get 一个请求拿齐 最新价/昨收/涨跌幅/名称/总市值/市净率,
#   实测 0.4-0.8s, 且比全市场表**多**给港股的总市值/市净率 (全市场 spot 的
#   最终列把这两个丢了)。数据口径仍是东财实时 (akshare 只是它的封装), 故
#   source 标签沿用 akshare_em; 新浪兜底另走 akshare_sina。

_AK_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 东财对无 UA 的请求直接 RST: akshare 的 stock_bid_ask_em 就是这样失败的
_AK_QUOTE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_AK_QUOTE_TIMEOUT = 8.0
# 东财字段码: f43=最新价 f57=代码 f58=名称 f60=昨收
#              f116=总市值 f162=市盈率(动) f167=市净率 f170=涨跌幅(%)
#              f172=报价币种
# f172 必须取: 港股有 USD/CNY 计价柜台 (如 09188/82318), 只按代码后缀
# 推断会把 USD 价格当 HKD 乘汇率, 市值差约 7.8 倍。
# f162 口径是「动态 PE」, 不是 TTM (实测 A 股可用, 部分港股返回 "-"):
# 单独写入 Quote.pe_dynamic, 不与估值页的 pe_ttm 混用。
_AK_QUOTE_FIELDS = "f43,f57,f58,f60,f116,f162,f167,f170,f172"

# 单代码行情进程内缓存 (短 TTL): 同一代码紧接着的重复请求 (UI 渲染 + 后台
# 刷新) 只发一次; per-key 单飞去重并发首拉; 失败写短 TTL 占位防反复打东财
_AK_QUOTE_TTL = 5.0
_ak_quote_cache: dict[str, tuple[float, dict | None]] = {}
_ak_quote_lock = threading.Lock()
_ak_quote_inflight: dict[str, threading.Lock] = {}


def _ak_secid(p: ParsedSymbol) -> str:
    """ParsedSymbol → 东财 secid (市场段.代码).

    段号按**交易所后缀**判 (不能按代码首位: 沪市 B 股 900xxx 与深市创业板
    300xxx 首位都非 6, 而东财把 900xxx 归在上海段 1):
      .SS/.SH → 1 (沪市: 60x/601/603/605/688 科创 + 900xxx B股)
      .SZ     → 0 (深市: 000/001/002/003/300/301 + 200xxx B股)
      .BJ     → 0 (北交所 43x/83x/87x/92x, 东财挂在 0 段)
      .HK     → 116 + 5 位代码 (00700/82318)
    """
    if p.market is Market.HK:
        return f"116.{p.ak_code}"
    code = p.ak_code or p.yahoo.split(".")[0]
    prefix = "1" if p.yahoo.rsplit(".", 1)[-1] in ("SS", "SH") else "0"
    return f"{prefix}.{code}"


#: 单代码失败的真实原因 (secid → (时间戳, 简短描述)). 只用于把健康记录里
#: 干巴巴的「akshare 行情不可用」补成可诊断的 HTTP/网络错误; 最近 2 分钟内
#: 的才使用, 避免旧错误串到下一次调用。
_AK_ERR_TTL = 120.0
_ak_quote_errors: dict[str, tuple[float, str]] = {}

#: 东财 push2 域名整段不可达时的进程级冷却: 连续 N 次网络失败后暂停回源,
#: 让 CN/HK 行情直接走新浪, 避免每轮保温对同一批代码重复等 3-5s。
#: 只统计网络/HTTP 失败; 200 + data=null (单只无效代码) 不算系统性故障。
#: 10 分钟 > 5 分钟保温周期, 保证至少跳过一轮; 恢复探测最多延迟 10 分钟。
_AK_QUOTE_THROTTLE_TTL = 600.0
_AK_QUOTE_THROTTLE_FAILS = 3
_ak_quote_throttle_lock = threading.Lock()
_ak_quote_fail_streak = 0
_ak_quote_block_until = 0.0


def akshare_quote_available() -> bool:
    """东财单代码行情是否可用 (连续网络失败后短暂冷却)."""
    with _ak_quote_throttle_lock:
        return time.time() >= _ak_quote_block_until


def reset_akshare_quote_throttle() -> None:
    """清空东财行情冷却 (测试隔离 / 设置页切换源时可选调用)."""
    global _ak_quote_fail_streak, _ak_quote_block_until
    with _ak_quote_throttle_lock:
        _ak_quote_fail_streak = 0
        _ak_quote_block_until = 0.0


def _ak_note_network_failure() -> None:
    global _ak_quote_fail_streak, _ak_quote_block_until
    with _ak_quote_throttle_lock:
        _ak_quote_fail_streak += 1
        if _ak_quote_fail_streak >= _AK_QUOTE_THROTTLE_FAILS:
            _ak_quote_block_until = time.time() + _AK_QUOTE_THROTTLE_TTL
            _ak_quote_fail_streak = 0


def _ak_note_endpoint_ok() -> None:
    global _ak_quote_fail_streak
    with _ak_quote_throttle_lock:
        _ak_quote_fail_streak = 0


def _err_text(exc: BaseException) -> str:
    """异常 → 单行诊断文案 (requests 的连接错误/超时都带 host)."""
    msg = " ".join(str(exc).split())
    name = type(exc).__name__
    text = f"{name}: {msg}" if msg else name
    return text[:200]


def _ak_note_error(secid: str, text: str | None) -> None:
    """记录/清除某代码最近一次东财失败原因."""
    with _ak_quote_lock:
        if text:
            _ak_quote_errors[secid] = (time.time(), text)
        else:
            _ak_quote_errors.pop(secid, None)


def _ak_quote_error(secid: str) -> str | None:
    """该代码最近一次东财失败原因 (过期返回 None)."""
    with _ak_quote_lock:
        hit = _ak_quote_errors.get(secid)
        if hit and time.time() - hit[0] < _AK_ERR_TTL:
            return hit[1]
    return None


def _ak_quote_fetch(secid: str) -> dict | None:
    """东财 push2 单代码行情 data 字典; 任何失败/无数据返回 None.

    失败原因写入 ``_ak_quote_errors``: 取数层只看到 None, 不记录就无法区分
    「网络被重置」「东财限流」「代码无行情」, 健康页会全部显示成同一句话。
    """
    try:
        r = _requests().get(
            _AK_QUOTE_URL,
            params={
                "fltt": "2",
                "invt": "2",
                "secid": secid,
                "fields": _AK_QUOTE_FIELDS,
            },
            headers=_AK_QUOTE_HEADERS,
            timeout=_AK_QUOTE_TIMEOUT,
        )
        if r.status_code >= 400:
            # 429/403 等限流/封禁响应: requests 不会自动抛错, 直接 .json()
            # 会退化成难以理解的 JSONDecodeError
            _ak_note_error(secid, f"HTTP {r.status_code}")
            _ak_note_network_failure()
            return None
        j = r.json()
    except Exception as e:  # noqa: BLE001 - 视作无行情, 细节留给健康记录
        _ak_note_error(secid, _err_text(e))
        _ak_note_network_failure()
        return None
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        # 东财对无效/退市代码也返回 200 + {"data": null}, 与网络错误区分开
        _ak_note_error(secid, "东财无该代码行情 (data=null)")
        _ak_note_endpoint_ok()
        return None
    _ak_note_error(secid, None)
    _ak_note_endpoint_ok()
    return data


def _ak_quote_raw(p: ParsedSymbol) -> dict | None:
    """带缓存/单飞/失败占位的单代码行情; 失败 None (调用方抛错降级下一源)."""
    secid = _ak_secid(p)
    now = time.time()
    with _ak_quote_lock:
        hit = _ak_quote_cache.get(secid)
        if hit:
            ts, data = hit
            if now - ts < (_AK_QUOTE_TTL if data else _AK_FAIL_TTL):
                return data
        gate = _ak_quote_inflight.setdefault(secid, threading.Lock())
    acquired = gate.acquire(blocking=False)
    try:
        if acquired:  # 本线程负责拉取; 其它线程等拉取方写回缓存后读结果
            data = _ak_quote_fetch(secid)
            with _ak_quote_lock:
                _ak_quote_cache[secid] = (now, data)
            return data
        gate.acquire()
        gate.release()  # 拉取方已 pop 此 gate: 立即释放, 不留持有者
    finally:
        if acquired:
            with _ak_quote_lock:
                if _ak_quote_inflight.get(secid) is gate:
                    _ak_quote_inflight.pop(secid, None)
            gate.release()
    # 未获 gate: 拉取方已完成, 重读缓存; 仍未命中 (失败) 则返回 None
    with _ak_quote_lock:
        hit = _ak_quote_cache.get(secid)
    if hit:
        ts, data = hit
        if now - ts < (_AK_QUOTE_TTL if data else _AK_FAIL_TTL):
            return data
    return None


def _ak_num(v) -> float | None:
    """东财字段值 → float; 缺失值 "-"/None/非数字 → None."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s in ("", "-", "None", "nan"):
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------- 沪深可转债兜底: 新浪单代码行情 ----------
#
# yfinance 完全不覆盖沪深可转债 (110081.SS 返回 delisted, 只有正股有数据)。
# 东财 stock/get 能取转债, 但 A 股全量保温时经常被临时限流; 此时可转债没有
# yfinance 兜底, 会稳定失败。新浪 hq.sinajs.cn 单代码行情与 akshare
# bond_zh_hs_cov_spot 同源, 一次只取一个代码 (约 0.3s, 无分页/进度条),
# 作为转债专用兜底。
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
_SINA_QUOTE_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": _AK_QUOTE_HEADERS["User-Agent"],
}
_SINA_QUOTE_TIMEOUT = 8.0
_SINA_QUOTE_TTL = 5.0
_SINA_FAIL_TTL = 15.0
_sina_quote_cache: dict[str, tuple[float, dict | None]] = {}
_sina_quote_lock = threading.Lock()


def _cn_bond_sina_symbol(p: ParsedSymbol) -> str | None:
    """沪深可转债 → 新浪单代码 (sh110081/sz128136); 非转债返回 None.

    代码段: 沪市 110/111/113/118, 深市 12xxxx。北交所暂无转债。
    """
    if p.market is not Market.CN:
        return None
    code = p.ak_code or p.yahoo.split(".")[0]
    if len(code) != 6 or not code.isdigit():
        return None
    suffix = p.yahoo.rsplit(".", 1)[-1].upper()
    if suffix in ("SS", "SH") and code[:3] in ("110", "111", "113", "118"):
        return f"sh{code}"
    if suffix == "SZ" and code[:2] == "12":
        return f"sz{code}"
    return None


def _sina_symbol(p: ParsedSymbol) -> str | None:
    """沪深/港股 → 新浪单代码 (sh600585/sz000001/rt_hk00700); 其它返回 None.

    北交所不在兜底范围: 新浪 ``bj`` 接口对北交所返回全 0 占位 (实测
    830799.BJ), 误判成有效行情会污染缓存; 北交所继续走东财/yfinance。
    港股用 ``rt_hk`` + 5 位代码 (含人民币柜台 8xxxx)。
    """
    if p.market is Market.HK:
        code = p.ak_code or p.yahoo.split(".")[0]
        return f"rt_hk{code}" if code.isdigit() else None
    if p.market is not Market.CN:
        return None
    code = p.ak_code or p.yahoo.split(".")[0]
    suffix = p.yahoo.rsplit(".", 1)[-1].upper()
    if suffix in ("SS", "SH"):
        return f"sh{code}"
    if suffix == "SZ":
        return f"sz{code}"
    return None


def _sina_quote_raw(p: ParsedSymbol) -> dict | None:
    """新浪单代码实时行情原始字段; 失败/无数据返回 None (带短 TTL 缓存).

    支持的返回字段: name / price / prev_close / change_pct (仅港股接口自带) /
    date / time。A股与港股的新浪字段布局不同, 必须按市场分别取:
      A股: 0=名称 2=昨收 3=最新价 ... 30=日期 31=时间
      港股 rt_hk: 0=英文名 1=中文名 3=昨收 6=最新价 8=涨跌幅 ... 17=日期 18=时间
    """
    symbol = _cn_bond_sina_symbol(p) or _sina_symbol(p)
    if not symbol:
        return None
    now = time.time()
    with _sina_quote_lock:
        hit = _sina_quote_cache.get(symbol)
        if hit and now - hit[0] < (_SINA_QUOTE_TTL if hit[1] else _SINA_FAIL_TTL):
            return hit[1]
    data = None
    try:
        r = _requests().get(
            _SINA_QUOTE_URL + symbol,
            headers=_SINA_QUOTE_HEADERS,
            timeout=_SINA_QUOTE_TIMEOUT,
        )
        r.encoding = "gb18030"  # 新浪行情是 GBK/GB18030, 默认 UTF-8 会乱码
        text = r.text or ""
        if '"' in text:
            fields = text.split('"', 2)[1].split(",")
            if p.market is Market.HK:
                # rt_hk 第 17/18 位是 2026/11/24 与 16:08:15
                if len(fields) >= 19 and (fields[1].strip() or fields[0].strip()):
                    data = {
                        "name": (fields[1].strip() or fields[0].strip()),
                        "prev_close": fields[3],
                        "price": fields[6],
                        "change_pct": fields[8],
                        "date": fields[17],
                        "time": fields[18],
                    }
            elif len(fields) >= 32 and fields[0].strip():
                data = {
                    "name": fields[0].strip(),
                    "prev_close": fields[2],
                    "price": fields[3],
                    "date": fields[30],
                    "time": fields[31],
                }
    except Exception:  # noqa: BLE001 - 网络/解析失败一律视为本源无行情
        data = None
    with _sina_quote_lock:
        _sina_quote_cache[symbol] = (now, data)
    return data


def _sina_bond_quote_raw(p: ParsedSymbol) -> dict | None:
    """新浪可转债实时行情原始字段 (保留旧入口, 字段解析与 A 股一致)."""
    return _sina_quote_raw(p)


def _quote_from_sina(
    p: ParsedSymbol,
    d: dict,
    *,
    source: str,
    currency: str | None = None,
    label: str = "新浪",
) -> Quote:
    """新浪原始字段 → Quote (价格守卫 + 涨跌幅回算), 供股票/转债共用."""
    price = _ak_num(d.get("price"))
    if price is None or price <= 0:
        raise RuntimeError(f"{p.yahoo}: {label}无有效最新价 ({d.get('price')!r})")
    prev = _ak_num(d.get("prev_close"))
    change_pct = _ak_num(d.get("change_pct"))
    if change_pct is None and prev is not None and prev > 0:
        change_pct = (price / prev - 1.0) * 100.0
    return Quote(
        symbol=p.yahoo,
        name=str(d.get("name") or "").strip() or None,
        price=price,
        prev_close=prev,
        change_pct=change_pct,
        currency=currency or p.currency,
        source=source,
    )


def _sina_quote(p: ParsedSymbol) -> Quote:
    """沪深/港股实时行情兜底 (新浪单代码); 东财 push2 不可达时接管."""
    d = _sina_quote_raw(p)
    if not d:
        raise RuntimeError(f"{p.yahoo}: 新浪行情不可用")
    return _quote_from_sina(p, d, source="akshare_sina")


def _sina_bond_quote(p: ParsedSymbol) -> Quote:
    """可转债行情 (新浪): 最新价 + 昨收 + 涨跌幅 + 名称.

    转债在东财链路里仍是 akshare_em 的内部兜底, 来源标签保持 akshare_em
    以兼容显式源偏好; 股票兜底另走 ``_sina_quote`` (source=akshare_sina)。
    """
    d = _sina_bond_quote_raw(p)
    if not d:
        raise RuntimeError(f"{p.yahoo}: 可转债行情不可用 (新浪)")
    return _quote_from_sina(
        p, d, source="akshare_em", currency="CNY", label="可转债"
    )


def _akshare_quote(p: ParsedSymbol) -> Quote:
    """A股/北交所/港股实时行情 (东财单代码): 价格 + 昨收 + 涨跌幅 + 名称.

    顺带反推每股净资产 (市净率) 与总市值 (东财实时口径, 比 yfinance 的滞后
    财报新), 供 PB/市值阈值提醒; 缺失即 None, 由 fundamentals 链路兜底。

    沪深可转债在东财失败/限流时回退新浪单代码行情; yfinance 不覆盖转债,
    没有这层兜底会稳定失败。
    """
    if not akshare_quote_available():
        # 源链通常会先过滤掉本源; 这里兜底覆盖显式顺序直接调用的情况
        raise RuntimeError(f"{p.yahoo}: 东财行情冷却中, 跳过 (akshare_em)")
    d = _ak_quote_raw(p)
    bond = _cn_bond_sina_symbol(p) is not None
    if d:
        # 停牌/无成交时东财最新价就是 "-": 与其余行情源一致地守卫, 无效即抛错
        # 回退下一源, 不让 0/NaN 报价污染盈亏/PB/涨跌幅
        price = _ak_num(d.get("f43"))
        if price is not None and price > 0:
            pb = _ak_num(d.get("f167"))
            market_cap = _ak_num(d.get("f116"))
            pe_dynamic = _ak_num(d.get("f162"))
            # 东财自报币种 (f172): 港股 USD/CNY 柜台不能按后缀推断成 HKD。
            # 但 A股/B股/北交所的 f172 是 "-" (币种由代码推导, B 股还可能被
            # 泛化), 必须用 parse 的权威币种; 不校验会把 "-" 写进行情缓存,
            # 汇率链随后把它拼成 "-CNY=X" 查到 USD/CNY 汇率, A 股市值放大数倍。
            reported_ccy = str(d.get("f172") or "").strip().upper()
            if p.market in (Market.CN, Market.BJ):
                currency = p.currency
            else:
                currency = (
                    reported_ccy if is_currency_code(reported_ccy) else p.currency
                )
            return Quote(
                symbol=p.yahoo,
                name=str(d.get("f58") or "").strip() or None,
                price=price,
                prev_close=_ak_num(d.get("f60")),
                change_pct=_ak_num(d.get("f170")),
                currency=currency,
                book_value=price / pb if pb is not None and pb > 0 else None,
                market_cap=(
                    market_cap if market_cap is not None and market_cap > 0 else None
                ),
                pe_dynamic=(
                    pe_dynamic if pe_dynamic is not None and pe_dynamic > 0 else None
                ),
                source="akshare_em",
            )
        if not bond:
            raise RuntimeError(f"{p.yahoo}: akshare 无有效最新价 ({d.get('f43')!r})")
    elif not bond:
        # 把真实原因 (连接被重置/超时/东财返回 data=null) 带出来: 健康记录
        # 里只有「akshare 行情不可用」时, 无法区分网络故障和数据源限流。
        detail = _ak_quote_error(_ak_secid(p))
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"{p.yahoo}: akshare 行情不可用 ({p.market.value}){suffix}")
    return _sina_bond_quote(p)


def _akshare_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    ak = _ak()
    start = start_date.replace("-", "")
    end = end_date.replace("-", "") if end_date else None
    bond_symbol = _cn_bond_sina_symbol(p)
    if bond_symbol:
        # 可转债日K: akshare 只有新浪接口 (全量历史, 无 start/end 参数),
        # 取回后按请求区间裁剪; 股票接口 stock_zh_a_hist 不支持转债代码.
        df = with_timeout(ak.bond_zh_hs_cov_daily, _AK_HIST_TIMEOUT, symbol=bond_symbol)
        if df is not None and not df.empty:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df[df["date"] >= pd.to_datetime(start_date)]
            if end_date:
                df = df[df["date"] <= pd.to_datetime(end_date)]
    elif p.market in (Market.CN, Market.BJ):
        kwargs = {
            "symbol": p.yahoo.split(".")[0],
            "period": "daily",
            "start_date": start,
            "adjust": "qfq",
        }
        if end:
            kwargs["end_date"] = end
        # 硬超时: akshare 的 requests.get 不传 timeout, TCP 半开时可挂到分钟级,
        # 而 get_history 跑在主线程 (CLI history/kline/compare) —— 整条命令卡死。
        df = with_timeout(ak.stock_zh_a_hist, _AK_HIST_TIMEOUT, **kwargs)
    else:
        ak_code = p.ak_code or ""
        if not ak_code or not ak_code.isdigit():
            raise ValueError(f"{p.yahoo}: 无效港股代码 (ak_code={ak_code!r})")
        kwargs = {
            "symbol": ak_code,
            "period": "daily",
            "start_date": start,
            "adjust": "qfq",
        }
        if end:
            kwargs["end_date"] = end
        df = with_timeout(ak.stock_hk_hist, _AK_HIST_TIMEOUT, **kwargs)
    # 三个分支统一守卫: akshare 无数据时返回完全无列的空表, 直接走到下面
    # rename/to_datetime 会抛裸 KeyError 'date' (原来只有 HK 分支有守卫,
    # A股分支的报错变成 "历史数据获取失败 ('date')", 看不出是「东财无该股
    # 历史」); 非空但缺日期/收盘列 (接口 schema 变更) 同样给可读错误。
    if df is None or df.empty:
        raise RuntimeError(f"{p.yahoo}: akshare 无历史数据")
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
    )
    if "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError(f"{p.yahoo}: akshare 历史数据缺少日期/收盘列")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    return df[keep]
