"""批量编排: 按域分组调用 provider, 聚合行情/历史结果.

输入混合代码列表 → parse → 按 provider 分组 → 各域独立取数 (IBKR 只注入全球域
与 A 股域的批量行情前置; 指数域无实时行情, 直接记 errors) → 汇总 quotes/errors/notes。
任一域失败不影响其它域。
逐代码源链回退并发执行 (全球域死代码单次 10s+ 耗时, 串行会拖死大批量场景);
取失败的代码进进程内负缓存, TTL 内跳过重试 (死代码每轮重烧 10s+ 无意义)。
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, Queue
from threading import Lock

import pandas as pd

from .. import cache as cache_mod
from ..symbols import Market, ParsedSymbol, parse as _parse_symbol
from .base import Quote, resolve
from .crypto import CryptoProvider
from .cn_stocks import CNStocksProvider
from .global_stocks import _yahoo_batch
from .index import IndexProvider

__all__ = ["get_quotes", "get_history", "neg_clear_all"]


def _parse_all(symbols) -> tuple[dict[str, ParsedSymbol], dict[str, str]]:
    by_yahoo: dict[str, ParsedSymbol] = {}
    errors: dict[str, str] = {}
    for s in symbols:
        try:
            p = _parse_symbol(s)
            by_yahoo.setdefault(p.yahoo, p)
        except ValueError as e:
            errors[str(s)] = str(e)
    return by_yahoo, errors


# ---------- 进程内负缓存: 全部源失败的代码, TTL 内不再重试 ----------

_NEG_TTL = cache_mod.CACHE_TTL  # 与实时行情缓存同周期 (5 分钟)
_neg_cache: dict[str, float] = {}
_neg_lock = Lock()
_FALLBACK_WORKERS = 8  # 逐代码源链回退并发数


def neg_cached(yahoo: str) -> bool:
    """代码是否处于负缓存 TTL 内 (get_quote 单代码路径共用)."""
    now = time.time()
    with _neg_lock:
        ts = _neg_cache.get(yahoo)
    return ts is not None and now - ts < _NEG_TTL


def neg_mark(yahoo: str) -> None:
    """标记代码全部源失败 (get_quote 单代码路径共用)."""
    with _neg_lock:
        _neg_cache[yahoo] = time.time()


def neg_clear(yahoo: str) -> None:
    """代码取数成功, 清出负缓存."""
    with _neg_lock:
        _neg_cache.pop(yahoo, None)


def neg_clear_all() -> None:
    """清空整个负缓存.

    数据源偏好/开关变化 (crypto_source / prefer_akshare / IBKR / 长桥) 时调用:
    负缓存只按代码记, 不记录失败发生在哪条源链下, 旧偏好下的失败会在 TTL 内
    继续挡住新链路 (设置页 clear_quotes() 只清了磁盘缓存, 漏了这里)。
    """
    with _neg_lock:
        _neg_cache.clear()




def _neg_filtered(
    plist: list[ParsedSymbol], errors: dict[str, str]
) -> list[ParsedSymbol]:
    """剔除负缓存命中项 (记入 errors), 返回真正需要取数的代码."""
    now = time.time()
    out: list[ParsedSymbol] = []
    with _neg_lock:
        expired = [k for k, ts in _neg_cache.items() if now - ts >= _NEG_TTL]
        for k in expired:
            _neg_cache.pop(k, None)
        hit = {k for k, ts in _neg_cache.items() if now - ts < _NEG_TTL}
    for p in plist:
        if p.yahoo in hit:
            errors[p.yahoo] = "近期全部数据源失败 (负缓存), 稍后自动重试"
        else:
            out.append(p)
    return out


def _fetch_domain(
    domain: str,
    plist: list[ParsedSymbol],
    prefer_akshare: bool,
    errors: dict[str, str],
    crypto_source: str = "auto",
    on_done=None,
) -> dict[str, Quote]:
    """一个域内并发走源链: 返回 {yahoo: Quote}, 失败写 errors + 负缓存.

    on_done(yahoo) 每结算一只回调一次 (成功/失败都算), 供首屏进度条推进。
    """
    provider = resolve({"global": "GLOBAL", "cn": "CN", "crypto": "CRYPTO"}[domain])

    def _one(p: ParsedSymbol):
        # 单代码失败捕获为异常值返回: pool.map 迭代时不中断其余代码。
        # 捕 BaseException 而非 Exception: util.with_timeout 会把 worker 里的
        # SystemExit/CancelledError 等原样透传, 只捕 Exception 会让它冲出
        # 收集循环, 把同批已成功的其它代码结果全部丢掉
        try:
            if domain == "crypto":
                # crypto 域 prefer_akshare 无意义, 参数位复用为源偏好 (字符串)
                return p.yahoo, provider.fetch_quote(p, prefer_first=crypto_source)
            if domain == "global":
                # 批量已尝试 yfinance: 这里走完整源链 (港股 akshare 兜底)
                return p.yahoo, provider.fetch_quote(p, prefer_first=prefer_akshare)
            return p.yahoo, provider.fetch_quote(p)
        except BaseException as e:  # noqa: BLE001 - 单代码失败记录 errors
            return p.yahoo, e

    out: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=_FALLBACK_WORKERS) as pool:
        for p, (yahoo, result_or_exc) in zip(
            plist, pool.map(_one, plist), strict=False
        ):
            if isinstance(result_or_exc, BaseException):
                errors[yahoo] = str(result_or_exc) or repr(result_or_exc)
                neg_mark(yahoo)
            else:
                out[yahoo] = result_or_exc
                neg_clear(yahoo)
            if on_done is not None:
                on_done(yahoo)
    return out


def _route_provider(p: ParsedSymbol):
    """ParsedSymbol → provider 实例 (按权威 type 字段路由)."""
    return resolve(p.type)


def _cached_source_ok(
    quote: Quote,
    parsed: ParsedSymbol,
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
) -> bool:
    """缓存行情是否满足本次显式数据源偏好 (未指定偏好时任何源都接受)."""
    # 同时开启多个显式源时, 缓存必须来自优先级最高的那个来源, 而不是
    # 「集合里任意一个」。否则 `--ibkr --longport` 只要缓存里是长桥数据
    # 就会在批量取数前提前返回, IBKR 根本没执行 (执行顺序是 IBKR 覆盖长桥)。
    required: str | None = None
    if use_ibkr and parsed.type == "global":
        required = "ibkr"
    elif use_longport and parsed.type in ("global", "cn") and not parsed.is_b_share:
        required = "longport"
    elif prefer_akshare and parsed.market is Market.HK:
        required = "akshare"
    elif parsed.type == "crypto" and crypto_source != "auto":
        # settings/CLI 用 gate 指 Gate.io, Quote.source 是 "gate.io"
        required = "gate.io" if crypto_source == "gate" else crypto_source
    return required is None or getattr(quote, "source", None) == required


def get_quotes(
    symbols,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
    progress=None,
    refresh: bool = False,
    source_strict: bool = False,
) -> tuple[dict[str, Quote], dict[str, str], list[str]]:
    """多代码实时行情: 缓存 → 长桥/IBKR 批量 (可选, IBKR 优先) → 按域 provider 取数 → 回写缓存.

    progress(done, total, label) 可选: 按已结算代码数推进, 供首屏冷启动
    进度条使用 (缓存命中/批量返回/逐代码成功或失败都计入 settled)。
    refresh=True: 跳过 5min 磁盘行情缓存, 并清掉这些代码的失败负缓存
    (CLI `quote --refresh` / 用户显式强制刷新; 不等 TTL 过期)。
    """
    quotes: dict[str, Quote] = {}
    notes: list[str] = []
    by_yahoo, errors = _parse_all(symbols)
    if not by_yahoo:
        return quotes, errors, notes
    total = len(by_yahoo)
    settled = set(quotes)

    def _emit(label: str | None = None) -> None:
        if progress is not None:
            progress(min(len(settled), total), total, label)

    if refresh:
        # 强制刷新: 负缓存只按代码记, 不清会让「上次失败」继续挡住本轮取数
        for yahoo in by_yahoo:
            neg_clear(yahoo)
    # 优先读本地缓存 (refresh 时跳过, 真正回源)
    cached = {} if refresh else cache_mod.get_cached(list(by_yahoo.keys()))
    if cached and source_strict:
        # 显式数据源偏好不能被缓存里的其它源结果短路: `quote AAPL --ibkr`
        # 在 TTL 内会直接命中上次 yfinance 的缓存, IBKR 根本没执行。只保留
        # 与本次偏好一致的缓存项, 其余交给下面的批量/源链重新取数。
        # source_strict 只由 CLI 显式 flag 打开; UI/设置页仍走缓存优先
        # (否则开启 IBKR 后每次首屏都会绕过磁盘缓存, 首屏秒开失效)。
        cached = {
            sym: q
            for sym, q in cached.items()
            if _cached_source_ok(
                q, by_yahoo[sym], prefer_akshare, use_ibkr, use_longport,
                crypto_source,
            )
        }
    if cached:
        quotes.update(cached)
        settled.update(cached)
        # 磁盘缓存命中同样是取数成功: 清出负缓存, 别让 TTL 内的旧失败
        # 继续挡住已能取到数的代码
        for yahoo in cached:
            neg_clear(yahoo)
    _emit("读取本地缓存")
    if len(quotes) == len(by_yahoo):
        _emit(None)
        return quotes, errors, notes

    # 可选批量前置: 长桥先取, IBKR 后取并覆盖 —— 两源都启用时 IBKR 优先级更高
    if use_longport:
        _emit("长桥批量行情")
        try:
            from .. import longport as lp_mod

            lp_quotes, reason = lp_mod.get_quotes_longport(list(by_yahoo.values()))
            quotes.update(lp_quotes)
            settled.update(lp_quotes)
            if reason:
                notes.append(f"长桥不可用: {reason} (已回退默认数据源)")
        except Exception as e:  # noqa: BLE001 - 长桥属可选增强
            notes.append(f"长桥接入异常: {e}")
        _emit("长桥批量行情")
    if use_ibkr:
        _emit("IBKR 批量行情")
        try:
            from .. import ibkr as ibkr_mod

            ib_quotes, reason = ibkr_mod.get_quotes_ibkr(list(by_yahoo.values()))
            quotes.update(ib_quotes)
            settled.update(ib_quotes)
            if reason:
                notes.append(f"IBKR 不可用: {reason} (已回退默认数据源)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            notes.append(f"IBKR 接入异常: {e}")
        _emit("IBKR 批量行情")
    groups: dict[str, list[ParsedSymbol]] = {}
    for p in by_yahoo.values():
        if p.yahoo in quotes:
            continue
        provider = _route_provider(p)
        if isinstance(provider, IndexProvider):
            # 指数域无实时行情 (仅历史K线): 显式记入 errors, 不进全球/股票源链
            errors.setdefault(
                p.yahoo, f"{p.yahoo}: 指数域仅提供历史K线 (index-kline), 无实时行情"
            )
            continue
        if isinstance(provider, CryptoProvider):
            groups.setdefault("crypto", []).append(p)
        elif isinstance(provider, CNStocksProvider):
            groups.setdefault("cn", []).append(p)
        else:
            groups.setdefault("global", []).append(p)

    # 各域先过滤负缓存 (全部源失败的代码 TTL 内不重试, 记 errors), 再取数。
    # 全球域: 剩余代码先批量 (yfinance), 批量未命中再逐个走源链 (域内并发)。
    # 负缓存前置到批量层: 批量请求同样包含这些死代码, 不剔除会整批重发并
    # 触发数据源限流 (单代码源链阶段原先才过滤, 批量层一直漏着)。
    #
    # 域间并发: cn/crypto 在各自线程取数, global 留在当前线程 —— global 的
    # 批量路径会首次初始化 OpenBB (signal 限制, 在非主线程可能失败), 且
    # IBKR 预取也只在主线程做。这样冷启动 600 只混合组合的等待从
    # 「global 批量 + cn + crypto」降到「三者最慢的一个」。
    # 代价: cn 域失败代码回落 yfinance 时会与 global 批量短暂叠加 Yahoo 压力;
    # 批量侧已有「连续空批早停」兜底 (_yahoo_batch), 未命中项会落逐代码源链。
    def _prepare(domain: str) -> list[ParsedSymbol]:
        return [
            p
            for p in _neg_filtered(groups.get(domain, []), errors)
            if p.yahoo not in quotes
        ]

    # 侧域完成事件经线程安全队列回到主线程: Streamlit 的 progress 只能在
    # 主线程调用, worker 里直接 _emit 会踩 ScriptRunner 上下文。
    side_done: Queue[str] = Queue()

    def _run_side(domain: str, plist: list[ParsedSymbol]):
        """侧域 (cn/crypto) 取数: 自己的 errors, 不碰共享状态."""
        errs: dict[str, str] = {}
        out = _fetch_domain(
            domain,
            plist,
            prefer_akshare,
            errs,
            crypto_source,
            on_done=side_done.put,
        )
        return out, errs

    def _drain_side_progress() -> None:
        while True:
            try:
                yahoo = side_done.get_nowait()
            except Empty:
                return
            settled.add(yahoo)
            _emit(yahoo)

    global_plist = _prepare("global")
    side_plists = {d: _prepare(d) for d in ("cn", "crypto")}

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote-domain") as pool:
        # future → domain (完成顺序与提交顺序无关)
        pending = {
            pool.submit(_run_side, domain, plist): domain
            for domain, plist in side_plists.items()
            if plist
        }

        if global_plist:
            # EODHD quotes=primary: 先走 EODHD 批量 (s= 参数, 每批 15 个),
            # 批量未命中的代码再回落 yfinance 批量/逐代码源链。EODHD 未启用或
            # 仅 fallback 时保持原有 yfinance 批量优化 (自选 600+ 必需)。
            from .. import eodhd as eodhd_mod
            from .. import source_order as so

            order = so.configured("global", "quote")

            def _order_pos(source_id: str) -> int | None:
                if order is None or source_id not in order:
                    return None
                return order.index(source_id)

            # 显式顺序是权威: 只有 EODHD 排在 yfinance 之前才做批量前置;
            # 排在后面时交给逐代码源链兜底, 不能抢先命中。
            eod_pos = _order_pos("eodhd")
            yf_pos = _order_pos("yfinance")
            if order is None:
                # 沿用旧行为 (含测试对 quote_primary 的 monkeypatch)
                eod_ready = eod_batch_mode = eodhd_mod.quote_primary()
            else:
                eod_cfg = eodhd_mod.load_config()
                eod_ready = (
                    eodhd_mod.available(eod_cfg)
                    and eod_cfg["quotes"] != "off"
                    and eod_pos is not None
                    and (yf_pos is None or eod_pos < yf_pos)
                )
                eod_batch_mode = eod_ready

            def _akshare_precedes(source_id: str) -> bool:
                """该批量源之前是否还有逐代码 akshare (港股必须让它先跑)."""
                # --akshare 是本次调用的显式偏好: 即使配置里 akshare 排在后面,
                # 也要让港股先走 akshare (与 provider 内临时前置保持一致)。
                if prefer_akshare:
                    return True
                if order is None:
                    return False
                ak_pos = _order_pos("akshare")
                if ak_pos is None:
                    return False
                pos = _order_pos(source_id)
                return pos is None or ak_pos < pos

            if eod_ready and eod_batch_mode:
                # 与 GlobalStocksProvider.quote_sources 保持一致: akshare 在
                # EODHD 之前时港股必须 akshare 先跑, 否则 EODHD 批量会抢先
                # 命中港股, 逐代码源链里的 akshare 永远没有机会执行。
                eod_plist = global_plist
                if _akshare_precedes("eodhd"):
                    eod_plist = [
                        p for p in global_plist if p.market is not Market.HK
                    ]
                if eod_plist:
                    _emit("EODHD 批量行情")
                    try:
                        eod_batch, eod_errors = eodhd_mod.quotes_batch(eod_plist)
                        quotes.update(eod_batch)
                        settled.update(eod_batch)
                        for err in eod_errors[:3]:
                            notes.append(f"EODHD 批量行情: {err}")
                    except Exception as e:  # noqa: BLE001 - 批量失败回落 yfinance
                        notes.append(f"EODHD 批量行情异常: {e} (已回退默认源)")
                    _emit("EODHD 批量行情")
                    global_plist = [
                        p for p in global_plist if p.yahoo not in quotes
                    ]

            # 港股 akshare 排在 yfinance 之前时不能让批量抢先: 批量的结果会
            # 直接进 quotes, 逐代码源链就不再执行, 港股永远走 yfinance。
            if order is None or "yfinance" in order:
                batch_plist = global_plist
                if _akshare_precedes("yfinance"):
                    batch_plist = [
                        p for p in global_plist if p.market is not Market.HK
                    ]
                if batch_plist:
                    _emit("yfinance 批量行情")
                    try:
                        batch = _yahoo_batch(batch_plist)
                        quotes.update(batch)
                        settled.update(batch)
                    except Exception as e:  # noqa: BLE001 - 批量失败不能让整个域崩
                        # 批量是优化不是必需: 失败则全部代码落逐代码源链 (那里每源
                        # 单独 try)。逐代码阶段异常也会被 _fetch_domain 收敛成单码错误。
                        notes.append(
                            f"yfinance 批量行情异常: {e} (已回退逐代码取数)"
                        )
                    _emit("yfinance 批量行情")
                    global_plist = [
                        p for p in global_plist if p.yahoo not in quotes
                    ]

            if global_plist:
                if progress is not None:

                    def _on_done(yahoo: str) -> None:
                        settled.add(yahoo)
                        _emit(yahoo)

                    quotes.update(
                        _fetch_domain(
                            "global",
                            global_plist,
                            prefer_akshare,
                            errors,
                            crypto_source,
                            on_done=_on_done,
                        )
                    )
                else:
                    quotes.update(
                        _fetch_domain(
                            "global",
                            global_plist,
                            prefer_akshare,
                            errors,
                            crypto_source,
                        )
                    )

        # 侧域此时多半已完成 (global 批量/回退是主耗时); 边等边把完成事件
        # 转成进度 (global 缺失时也能逐只推进), 异常不拖垮其它域。
        _drain_side_progress()
        while pending:
            done_futs, _ = wait(
                list(pending), timeout=0.1, return_when=FIRST_COMPLETED
            )
            _drain_side_progress()
            for fut in done_futs:
                domain = pending.pop(fut)
                try:
                    out, errs = fut.result()
                except Exception as e:  # noqa: BLE001 - 单域异常不影响其它域
                    notes.append(f"{domain} 行情取数异常: {e}")
                    continue
                quotes.update(out)
                errors.update(errs)
        _drain_side_progress()

    # 只回写本次真正取到的新鲜行情 (IBKR/akshare/yahoo), 缓存命中项不重写,
    # 否则 set_cached 会刷新其 fetched_at, TTL 被无限延长
    fresh = {
        sym: q for sym, q in quotes.items() if sym in by_yahoo and sym not in cached
    }
    cache_mod.set_cached(fresh)
    # 统一的负缓存清除点: 逐代码源链 (_fetch_domain) 之外, yfinance 批量/
    # IBKR/长桥路径拿到的成功行情同样要清, 否则源恢复后仍被 TTL 挡住
    for yahoo in quotes:
        neg_clear(yahoo)
    _emit(None)
    return quotes, errors, notes


def get_history(
    symbol: str,
    months: int = 12,
    start_date: str | None = None,
    end_date: str | None = None,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
) -> pd.DataFrame:
    """单代码历史K线: 长桥/IBKR (可选) → 所属 provider 源链."""
    from datetime import date, timedelta

    p = _parse_symbol(symbol)
    if start_date is None:
        start_date = (date.today() - timedelta(days=months * 31)).isoformat()
    if end_date is None:
        end_date = date.today().isoformat()
    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            ib_hist, reason = ibkr_mod.get_history_ibkr(
                [p], months, start_date=start_date, end_date=end_date
            )
            if p.yahoo in ib_hist:
                return ib_hist[p.yahoo]
            if reason:
                import warnings

                warnings.warn(f"IBKR 历史数据不可用: {reason} (已回退)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            import warnings

            warnings.warn(f"IBKR 历史数据异常: {e}")
    if use_longport:
        try:
            from .. import longport as lp_mod

            lp_hist = lp_mod.get_history_longport(p, start_date, end_date)
            if lp_hist is not None:
                return lp_hist
        except Exception as e:  # noqa: BLE001 - 长桥属可选增强
            import warnings

            warnings.warn(f"长桥历史数据异常: {e}")
    provider = _route_provider(p)
    # crypto 域: prefer_akshare 无意义, 复用参数位传源偏好 (字符串); 其它域原样布尔
    prefer = crypto_source if p.type == "crypto" else prefer_akshare
    return provider.fetch_history(p, start_date, end_date, prefer_first=prefer)
