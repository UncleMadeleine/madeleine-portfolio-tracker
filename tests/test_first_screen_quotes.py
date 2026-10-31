"""首屏数据 stale-while-revalidate 测试: 陈旧数据秒渲染 + 后台刷新 + 看门狗 rerun.

四层覆盖:
1. cache.get_cached_with_age / get_fx_cached: 新鲜/陈旧/越界三档读取与 age 口径;
2. fx.get_fx_rates 磁盘读穿: 新鲜期内不发网络, 缺失项走源链并回写;
3. 组合页首屏 (AppTest 跑真实 app.py):
   - 行情陈旧 (5min<age≤30min) → 立即渲染旧数据, 不阻塞在网络; 后台线程
     重取并落库; 看门狗 rerun 后换上新鲜数据;
   - 行情部分缺失 → 可用代码秒渲染, 缺失代码报「行情缺失」, 后台补齐;
   - 行情全新 (age≤5min) → 直接渲染磁盘数据, 不发任何取数调用;
   - 汇率陈旧 → 旧汇率秒算市值, 后台刷新汇率后看门狗 rerun 换新;
   - 行情/汇率全空 (冷启动) → 保持阻塞拉取 (没有可展示的数据);
4. 刷新协调器: 同 key 不并发、失败也记完成时间、最短重试间隔节流、
   一个任务同时刷行情+汇率、键随任一侧参数变化。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from streamlit.testing.v1 import AppTest

import tracker.cache as cache_mod
import tracker.fx as fx_mod
import tracker.prices as prices
import tracker.storage as storage
import tracker.ui.app as app_mod
import tracker.ui.portfolio_page as portfolio_page
from tracker.prices import Quote


def _wait_for(pred, timeout: float = 10.0, sleep_fn=None) -> bool:
    """轮询等待后台条件 (测试里替代 sleep 定长).

    sleep_fn 可注入: 打桩了 time.sleep 的用例 (如保温循环测试) 传真实 sleep,
    否则等待本身会踩到打桩函数。
    """
    sleep_fn = sleep_fn or time.sleep
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        sleep_fn(0.05)
    return False


class _StopLoop(BaseException):
    """跳出保温线程 while True 的信号.

    派生自 BaseException 而非 Exception: 循环体内 except Exception 捕不掉,
    才能穿透出来; 也不用 KeyboardInterrupt/SystemExit —— 那俩会被 pytest 和
    xdist 当成"中断整个会话"。
    """


@pytest.fixture
def _clean_quote_state():
    """跨测试隔离: st.cache_data 行情条目 + 模块级刷新协调器状态."""
    portfolio_page.cached_quotes.clear()
    portfolio_page.cached_fx.clear()
    with portfolio_page._refresh_lock:
        portfolio_page._refresh_inflight.clear()
        portfolio_page._refresh_done_at.clear()
    yield
    portfolio_page.cached_quotes.clear()
    portfolio_page.cached_fx.clear()
    with portfolio_page._refresh_lock:
        portfolio_page._refresh_inflight.clear()
        portfolio_page._refresh_done_at.clear()


def _seed_quotes(
    symbols_prices: dict[str, float],
    age_seconds: float,
    currencies: dict[str, str] | None = None,
) -> None:
    """往隔离缓存库写入行情, 再把 fetched_at 拨到 age_seconds 秒前.

    currencies 缺省全部按 USD (多数用例只关心价格); 需要本币/多币种持仓
    场景 (如 base=CNY 的 A股) 显式传入。
    """
    currencies = currencies or {}
    cache_mod.set_cached(
        {
            s: Quote(
                symbol=s,
                name=s,
                price=p,
                prev_close=p - 1.0,
                change_pct=0.5,
                currency=currencies.get(s, "USD"),
            )
            for s, p in symbols_prices.items()
        }
    )
    fetched_at = time.time() - age_seconds
    with sqlite3.connect(cache_mod.CACHE_DB) as con:
        for s in symbols_prices:
            con.execute(
                "UPDATE quotes SET fetched_at=? WHERE symbol=?", (fetched_at, s)
            )
        con.commit()


def _seed_fx(base: str, rates: dict[str, float], age_seconds: float) -> None:
    """往隔离缓存库写入汇率, 再把 fetched_at 拨到 age_seconds 秒前."""
    cache_mod.set_fx_cached(base, rates)
    fetched_at = time.time() - age_seconds
    with sqlite3.connect(cache_mod.CACHE_DB) as con:
        for ccy in rates:
            con.execute(
                "UPDATE fx_rates SET fetched_at=? WHERE base=? AND currency=?",
                (fetched_at, base.upper(), ccy.upper()),
            )
        con.commit()


def _reset_coordinator() -> None:
    """清空刷新协调器状态 (同一测试内多场景间隔离, 避开最短重试间隔)."""
    with portfolio_page._refresh_lock:
        portfolio_page._refresh_inflight.clear()
        portfolio_page._refresh_done_at.clear()


def _wait_coordinator_idle(sleep_fn) -> bool:
    """等所有后台刷新线程跑完 (done_at 落账), 避免场景间状态竞争."""
    return _wait_for(
        lambda: not portfolio_page._refresh_inflight,
        sleep_fn=sleep_fn,
    )


def _recording_quotes(price: float):
    """复刻真实 get_quotes 契约的 mock: 返回行情 + 落库 (后台线程会调用).

    挂在函数属性上的 calls 记录 (符号集, 调用时刻), 供断言刷新确已发生。
    """
    calls: list[tuple[tuple[str, ...], float]] = []
    def fake(symbols, **_kw):
        calls.append((tuple(symbols), time.monotonic()))
        quotes = {
            s: Quote(
                symbol=s,
                name="Apple",
                price=price,
                prev_close=price - 2.0,
                change_pct=0.01,
                currency="USD",
            )
            for s in symbols
        }
        cache_mod.set_cached(quotes)
        return quotes, {}, []

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _recording_fx(rate: float):
    """复刻真实 get_fx_rates 契约的 mock: 返回汇率 + 落库."""
    calls: list[tuple] = []

    def fake(base, currencies, **_kw):
        calls.append((base, tuple(currencies)))
        rates = {c: rate for c in currencies if c != base.upper()}
        cache_mod.set_fx_cached(base, rates)
        return rates, []

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _boot(monkeypatch, tmp_path, holdings=None, get_quotes=None, get_fx=None):
    """以指定持仓启动 app.py 完整渲染组合页.

    get_quotes/get_fx 缺省时不打桩 (真实实现): 磁盘全新路径下不会被调用,
    冷启动路径则会走真实取数 —— 调用方按测试意图显式传 mock。
    """
    wl = tmp_path / "watchlist.json"
    pf = tmp_path / "portfolio.json"
    storage.save_watchlist({"watchlist": []}, wl)
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": holdings
            or [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        },
        pf,
    )
    monkeypatch.setattr(portfolio_page, "WATCHLIST_PATH", wl)
    monkeypatch.setattr(storage, "PORTFOLIO_PATH", pf)
    if get_fx is not None:
        monkeypatch.setattr(portfolio_page, "get_fx_rates", get_fx)
    else:
        # 冷启动汇率兜底: 秒回不落库 (fx 相关的测试自行打桩)
        monkeypatch.setattr(
            portfolio_page, "get_fx_rates", lambda base, curs, **kw: ({"USD": 7.1}, [])
        )
    if get_quotes is not None:
        monkeypatch.setattr(prices, "get_quotes", get_quotes)
    at = AppTest.from_file(app_mod.__file__).run(timeout=60)
    assert not at.exception, at.exception
    return at


def _plan_dirty(at) -> bool:
    """AppTest 的 session_state 是 SafeSessionState (无 .get), 用 in + 索引."""
    if portfolio_page._PLAN_KEY not in at.session_state:
        return False
    return bool(at.session_state[portfolio_page._PLAN_KEY].get("dirty"))


class TestCachedWithAge:
    """磁盘读取的三档语义: 新鲜 / 陈旧可渲染 / 越界 miss."""

    def test_fresh_entry_age_near_zero(self):
        _seed_quotes({"AAPL": 150.0}, age_seconds=10)
        hits, ages = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].price == 150.0
        assert ages["AAPL"] < 60
        # 新鲜期内老接口同样命中
        assert "AAPL" in cache_mod.get_cached(["AAPL"])

    def test_stale_entry_readable_with_age(self):
        _seed_quotes({"AAPL": 150.0}, age_seconds=600)  # 5min < 600s < 30min
        hits, ages = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].price == 150.0
        assert 550 <= ages["AAPL"] <= 650
        # 超过新鲜期: 老接口 (默认 CACHE_TTL) 不再返回 —— 首屏据此判定需后台刷新
        assert cache_mod.get_cached(["AAPL"]) == {}

    def test_beyond_stale_window_missed(self):
        _seed_quotes({"AAPL": 150.0}, age_seconds=cache_mod.STALE_TTL + 60)
        assert cache_mod.get_cached_with_age(["AAPL"]) == ({}, {})
        assert cache_mod.get_cached(["AAPL"]) == {}

    def test_empty_input_and_bad_price(self):
        assert cache_mod.get_cached_with_age([]) == ({}, {})
        cache_mod.set_cached(
            {
                "BAD": Quote(
                    symbol="BAD",
                    name="",
                    price=-1.0,
                    prev_close=None,
                    change_pct=None,
                    currency="USD",
                )
            }
        )
        assert cache_mod.get_cached_with_age(["BAD"]) == ({}, {})

    def test_info_reports_stale_window(self):
        _seed_quotes({"AAPL": 150.0}, age_seconds=600)
        info = cache_mod.info()
        assert info["ttl_seconds"] == cache_mod.CACHE_TTL
        assert info["stale_ttl_seconds"] == cache_mod.STALE_TTL
        # 600s 的条目: 不新鲜但仍在陈旧可渲染档
        assert info["fresh"] == 0
        assert info["usable"] == 1


class TestFxDiskCache:
    """汇率磁盘缓存: 三档读取 + get_fx_rates 读穿 + base 自身不落库."""

    def test_fresh_stale_and_beyond_window(self):
        _seed_fx("CNY", {"USD": 7.1, "HKD": 0.85}, age_seconds=10)
        rates, ages = cache_mod.get_fx_cached("CNY", ["USD", "HKD", "EUR"])
        assert rates == {"USD": 7.1, "HKD": 0.85}
        assert ages["USD"] < 60
        # base 自身 (CNY→CNY) 不入库也不返回
        assert "CNY" not in rates
        rates, _ = cache_mod.get_fx_cached("CNY", ["USD"], ttl=cache_mod.FX_FRESH_TTL)
        assert rates == {"USD": 7.1}

    def test_base_itself_returns_empty(self):
        assert cache_mod.get_fx_cached("CNY", ["CNY"]) == ({}, {})

    def test_get_fx_rates_read_through(self, monkeypatch):
        """磁盘新鲜期内 get_fx_rates 不发网络; 缺失币种走源链并回写."""
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=10)
        calls: list[str] = []

        def fake_get_rate(src, dst, use_ibkr=False):
            calls.append(src)
            return 0.85  # HKD→CNY

        monkeypatch.setattr(fx_mod, "get_rate", fake_get_rate)
        rates, missing = fx_mod.get_fx_rates("CNY", ["USD", "HKD", "JPY"])
        # base 自身不在请求列表里 → 不出现 (与原语义一致: 仅请求 base 时才给 1.0)
        assert rates == {"USD": 7.1, "HKD": 0.85, "JPY": 0.85}
        assert missing == []
        # USD 命中磁盘未走源链; HKD/JPY 走了
        assert calls == ["HKD", "JPY"]
        # 新取的汇率回写磁盘
        rates2, _ = cache_mod.get_fx_cached("CNY", ["HKD", "JPY"])
        assert rates2 == {"HKD": 0.85, "JPY": 0.85}

    def test_get_fx_rates_base_in_currencies_gets_one(self, monkeypatch):
        """请求列表含 base 自身时返回 1.0 (build_view 查本币持仓汇率)."""
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=10)
        rates, missing = fx_mod.get_fx_rates("CNY", ["CNY", "USD"])
        assert rates == {"CNY": 1.0, "USD": 7.1}
        assert missing == []


class TestFirstScreenStaleWhileRevalidate:
    def test_stale_quotes_render_instantly_then_background_refresh(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """行情陈旧: 首屏秒渲染旧价 (不等网络), 后台刷新落库后看门狗 rerun 换新."""
        _seed_quotes({"AAPL": 100.0}, age_seconds=600)
        fake = _recording_quotes(200.0)
        at = _boot(monkeypatch, tmp_path, get_quotes=fake)

        # 陈旧价 2 股 × 100 × 7.1 = 1,420 (若阻塞等刷新会是 2,840)
        assert any("1,420" in str(m.value) for m in at.metric)
        # 刷新计划已挂起, 看门狗给出状态提示
        assert _plan_dirty(at)
        assert any("后台刷新中" in c.value for c in at.caption)

        # 后台线程确实重取并把新价写回磁盘
        assert _wait_for(lambda: fake.calls), "后台刷新未被触发"
        hits, _ = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].price == 200.0

        # 看门狗发现刷新完成 → rerun → 首屏换上新鲜价 2 股 × 200 × 7.1 = 2,840
        at.run(timeout=60)
        assert any("2,840" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)

    def test_partial_quotes_render_available_then_fill_missing(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """AAPL 陈旧 + MSFT 缺失: 秒渲染 AAPL, MSFT 报缺失, 后台补齐后完整."""
        _seed_quotes({"AAPL": 100.0}, age_seconds=600)
        fake = _recording_quotes(200.0)
        holdings = [
            {"symbol": "AAPL", "quantity": 2, "avg_cost": 100},
            {"symbol": "MSFT", "quantity": 1, "avg_cost": 300},
        ]
        at = _boot(monkeypatch, tmp_path, holdings=holdings, get_quotes=fake)

        # 只有 AAPL 可用: 2 × 100 × 7.1 = 1,420; MSFT 走「行情缺失」分支
        assert any("1,420" in str(m.value) for m in at.metric)
        assert _wait_for(lambda: fake.calls)
        at.run(timeout=60)
        # 两个代码都到位: (2×200 + 1×200) × 7.1 = 4,260
        assert any("4,260" in str(m.value) for m in at.metric)

    def test_fresh_quotes_render_from_disk_without_any_fetch(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """行情全新: 直接渲染磁盘数据, 不打桩的 get_quotes 一次都不该被调用."""
        _seed_quotes({"AAPL": 100.0}, age_seconds=30)

        def boom(symbols, **_kw):
            raise AssertionError("全新磁盘不应触发取数")

        at = _boot(monkeypatch, tmp_path, get_quotes=boom)
        assert any("1,420" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)
        assert not any("后台刷新中" in c.value for c in at.caption)

    def test_stale_fx_renders_instantly_then_background_refresh(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """汇率陈旧 (行情全新): 旧汇率秒算市值, 后台刷汇率后看门狗 rerun 换新.

        合并任务会同时刷行情+汇率两个槽位: 行情槽位此时是零网络成本的空转
        (磁盘全新, get_quotes 读盘即返回), 用 spy 委托真实实现来证明不打网络。
        """
        _seed_quotes({"AAPL": 100.0}, age_seconds=30)  # 行情全新
        _seed_fx("CNY", {"USD": 5.0}, age_seconds=600)  # 汇率陈旧
        fake_fx = _recording_fx(7.1)
        real_get_quotes = prices.get_quotes
        q_calls: list[tuple] = []

        def spy_quotes(symbols, **kw):
            q_calls.append(tuple(symbols))
            return real_get_quotes(symbols, **kw)  # 磁盘全命中, 不打网络

        at = _boot(monkeypatch, tmp_path, get_quotes=spy_quotes, get_fx=fake_fx)

        # 旧汇率 2 × 100 × 5.0 = 1,000 (若阻塞等刷新会是 1,420)
        assert any("1,000" in str(m.value) for m in at.metric)
        assert _plan_dirty(at)

        # 后台任务刷汇率 (行情槽位空转), 落库后看门狗 rerun → 新汇率 1,420
        assert _wait_for(lambda: fake_fx.calls), "后台汇率刷新未被触发"
        assert q_calls == [("AAPL",)]  # 行情槽位确实跑了, 但没打网络
        rates, _ = cache_mod.get_fx_cached("CNY", ["USD"])
        assert rates["USD"] == 7.1
        at.run(timeout=60)
        assert any("1,420" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)

    def test_periodic_keep_warm_updates_open_page(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """页面级周期保温: 数据 250s 龄 (仍新鲜但已过 240s 保温周期).

        首屏按新鲜数据渲染 (不走 SWR 脏路径), 看门狗到时主动重取 → 完成后
        rerun 换新 —— 页面一直开着数据也持续滚动更新。
        """
        _seed_quotes({"AAPL": 100.0}, age_seconds=250)
        fake = _recording_quotes(200.0)
        at = _boot(monkeypatch, tmp_path, get_quotes=fake)

        # 250s < 300s 新鲜期: 首屏直接渲染这批数据 (2×100×7.1=1,420)
        assert any("1,420" in str(m.value) for m in at.metric)
        # 但已过保温周期 → 看门狗挂上刷新任务并给提示
        assert any("后台刷新中" in c.value for c in at.caption)
        assert _wait_for(lambda: fake.calls), "周期保温未触发刷新"
        hits, _ = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].price == 200.0

        # 完成 → rerun → 换新 (2×200×7.1=2,840), 计划归位
        at.run(timeout=60)
        assert any("2,840" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)
        # 数据刚刷新生效, 不会立刻再刷一次 (无热循环)
        assert not any("后台刷新中" in c.value for c in at.caption)

    def test_no_periodic_refresh_while_data_is_young(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """数据 30s 龄 (远未到保温周期): 页面安静, 不发起任何刷新."""
        _seed_quotes({"AAPL": 100.0}, age_seconds=30)
        fake = _recording_quotes(200.0)
        at = _boot(monkeypatch, tmp_path, get_quotes=fake)
        assert any("1,420" in str(m.value) for m in at.metric)
        assert fake.calls == []
        assert not any("后台刷新中" in c.value for c in at.caption)

    def test_cold_start_blocks_and_fetches(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """空磁盘 (冷启动): 没有可展示数据, 保持阻塞拉取, 不挂后台任务."""
        fake = _recording_quotes(150.0)
        at = _boot(monkeypatch, tmp_path, get_quotes=fake)
        assert fake.calls  # 同步走完 cached_quotes
        assert any("2,130" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)

    def test_cold_start_reports_progress(self, monkeypatch, tmp_path, _clean_quote_state):
        """冷启动阻塞取数带 progress 回调 (done/total/label 推进到 100%)."""
        seen: list[tuple[int, int, str | None]] = []

        def fake(symbols, progress=None, **_kw):
            from tracker.providers.base import Quote

            assert progress is not None, "冷启动应挂进度回调"
            progress(0, len(symbols), "读取本地缓存")
            seen.append((len(symbols), len(symbols), None))
            progress(len(symbols), len(symbols), None)
            return (
                {
                    s: Quote(
                        symbol=s,
                        name="Apple",
                        price=150.0,
                        prev_close=148.0,
                        change_pct=0.01,
                        currency="USD",
                    )
                    for s in symbols
                },
                {},
                [],
            )

        at = _boot(monkeypatch, tmp_path, get_quotes=fake)
        assert seen == [(1, 1, None)]
        # 进度条取完即移除, 页面仍应完整渲染
        assert any("2,130" in str(m.value) for m in at.metric)

    def test_base_currency_holding_does_not_dirty_plan(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """回归: 持有本币资产 (base=CNY) 时计划不得永久 dirty.

        base 自身即 1.0 且不落盘, 若把 base 也纳入陈旧判定, 只要持有本币
        (极常见) 刷新计划就永远脏 → 看门狗无限 rerun 热循环。
        """
        _seed_quotes(
            {"AAPL": 100.0, "600519.SS": 333.0},
            age_seconds=30,
            currencies={"AAPL": "USD", "600519.SS": "CNY"},
        )
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=30)

        def boom(symbols, **_kw):
            raise AssertionError("数据全新不应触发取数")

        holdings = [
            {"symbol": "AAPL", "quantity": 2, "avg_cost": 100},
            {"symbol": "600519.SS", "quantity": 1, "avg_cost": 1500},
        ]
        at = _boot(monkeypatch, tmp_path, holdings=holdings, get_quotes=boom)
        # 2×100×7.1 + 333×1 = 1,753; 本币持仓按 1.0 换算, 不报「汇率缺失」
        assert any("1,753" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)
        assert not any("汇率缺失" in str(e.value) for e in at.error)
        assert not any("后台刷新中" in c.value for c in at.caption)

    def test_failed_refresh_is_throttled_not_hot_looping(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """回归: 刷新完成后数据仍陈旧 → 完成事件只消费一次 + 按间隔重试.

        修复前: 刷新完成后 rerun, 新渲染里 base 时间戳丢失 (done>0) 又立刻
        rerun, 形成不节流的热循环。
        """
        _seed_quotes({"AAPL": 100.0}, age_seconds=600)

        def boom(symbols, **_kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(prices, "get_quotes", boom)
        at = _boot(monkeypatch, tmp_path, get_quotes=boom)

        # 等后台刷新失败完成 (done 落账), 再rerun: 完成事件被消费, 且因最短
        # 间隔未到不再发起 → 无「刷新中」caption, 也不会持续 rerun
        key_plan = at.session_state[portfolio_page._PLAN_KEY]
        assert _wait_for(
            lambda: portfolio_page._refresh_state(
                portfolio_page._plan_key(
                    {"quotes": key_plan["quotes"], "fx": key_plan["fx"]}
                )
            )[1]
            is not None
        ), "后台刷新未记录完成时间"
        at.run(timeout=60)
        plan = at.session_state[portfolio_page._PLAN_KEY]
        assert plan["dirty"] is True  # 数据仍陈旧, 计划仍脏
        assert plan["seen"] > 0  # 完成事件已消费 (不会反复 rerun)
        assert not any("后台刷新中" in c.value for c in at.caption)


class TestPeriodicKeepWarm:
    """周期保温: 页面长期打开数据滚动更新 + 进程级线程在无页面时刷缓存.

    判定口径是磁盘数据年龄 (不是距上次刷新的时间): 冷启动刚取完数 / 全新
    打开都不触发, 数据临近过期才刷 —— 与首屏 SWR 的新鲜期天然衔接。
    """

    def test_period_scales_with_symbol_count(self):
        # 100 个代码: 100*2=200 < 基准 240 → 仍为基准
        assert portfolio_page._refresh_period(100) == 240.0
        # 685 个代码: 685*2=1370, 介于基准与上限之间
        assert portfolio_page._refresh_period(685) == 1370.0
        # 超大组合封顶在上限 (= 陈旧期)
        assert portfolio_page._refresh_period(10**6) == (
            portfolio_page._REFRESH_PERIOD_MAX
        )
        assert portfolio_page._refresh_period(1) == portfolio_page._REFRESH_PERIOD

    def test_cache_age_missing_means_inf(self):
        assert portfolio_page._cache_age() == 0.0
        assert portfolio_page._cache_age(symbols=["AAPL"]) == float("inf")
        _seed_quotes({"AAPL": 100.0}, age_seconds=120)
        assert 100 <= portfolio_page._cache_age(symbols=["AAPL"]) <= 180
        # 行情在、汇率缺 → inf (必须刷)
        assert portfolio_page._cache_age(
            symbols=["AAPL"], fx=("CNY", ("USD",), False)
        ) == float("inf")
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=60)
        age = portfolio_page._cache_age(symbols=["AAPL"], fx=("CNY", ("USD",), False))
        assert 100 <= age <= 180  # 取两者最旧

    def test_disk_refresh_plan_derives_from_storage(self, monkeypatch, tmp_path):
        """后台线程的取数参数从磁盘数据推导 (持仓+自选+设置), 币种取自行情缓存."""
        wl = tmp_path / "watchlist.json"
        pf = tmp_path / "portfolio.json"
        storage.save_portfolio(
            {
                "base_currency": "CNY",
                "holdings": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 100}],
            },
            pf,
        )
        storage.save_watchlist(
            {"watchlist": [{"symbol": "600519.SS", "lists": ["测试"]}]}, wl
        )
        monkeypatch.setattr(portfolio_page, "WATCHLIST_PATH", wl)
        monkeypatch.setattr(storage, "PORTFOLIO_PATH", pf)
        _seed_quotes(
            {"AAPL": 100.0, "600519.SS": 1500.0},
            age_seconds=60,
            currencies={"AAPL": "USD", "600519.SS": "CNY"},
        )
        plan = portfolio_page._disk_refresh_plan()
        symbols, prefer, ibkr, lp, crypto = plan["quotes"]
        assert symbols == ("AAPL", "600519.SS")
        # load_settings() 默认参数定义时绑定, 读的是真实 settings.json;
        # 这里只验证透传一致, 不断言具体开关值
        s = storage.load_settings()
        assert (prefer, ibkr, lp, crypto) == (
            s["prefer_akshare"],
            s["use_ibkr"],
            bool(s.get("use_longport")),
            s["crypto_source"],
        )
        base, currencies, _use_ibkr = plan["fx"]
        assert base == "CNY"
        assert currencies == ("USD",)  # CNY 是 base, 不进汇率槽位

    def test_disk_refresh_plan_none_when_empty(self, monkeypatch, tmp_path):
        wl = tmp_path / "watchlist.json"
        pf = tmp_path / "portfolio.json"
        storage.save_portfolio({"base_currency": "CNY", "holdings": []}, pf)
        storage.save_watchlist({"watchlist": []}, wl)
        monkeypatch.setattr(portfolio_page, "WATCHLIST_PATH", wl)
        monkeypatch.setattr(storage, "PORTFOLIO_PATH", pf)
        assert portfolio_page._disk_refresh_plan() is None

    def test_background_loop_refreshes_only_when_data_old(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """保温循环: 数据新 → 跳过; 数据旧 → 刷新 (patch sleep 跳出 while)."""
        wl = tmp_path / "watchlist.json"
        pf = tmp_path / "portfolio.json"
        storage.save_portfolio(
            {
                "base_currency": "CNY",
                "holdings": [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
            },
            pf,
        )
        storage.save_watchlist({"watchlist": []}, wl)
        monkeypatch.setattr(portfolio_page, "WATCHLIST_PATH", wl)
        monkeypatch.setattr(storage, "PORTFOLIO_PATH", pf)
        fake = _recording_quotes(200.0)
        monkeypatch.setattr(prices, "get_quotes", fake)
        # 汇率也要 mock: 否则合并任务会真去跑 CFETS/IBKR (仓库 settings 里
        # IBKR 开着, 连接拒绝 + akshare 兜底要 10s+)
        monkeypatch.setattr(
            portfolio_page,
            "get_fx_rates",
            lambda base, curs, **kw: ({c: 7.1 for c in curs if c != base}, []),
        )

        sleeps = {"n": 0}
        real_sleep = time.sleep

        def fake_sleep(_sec):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:  # 第二周期前跳出 while True
                raise _StopLoop

        monkeypatch.setattr(time, "sleep", fake_sleep)

        # 数据很新 (行情 60s + 汇率 60s, 均 < 240s 周期): 第一周期跳过
        _reset_coordinator()
        _seed_quotes({"AAPL": 100.0}, age_seconds=60)
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=60)
        sleeps["n"] = 0  # 每次调用只允许跑完一个周期 body
        with pytest.raises(_StopLoop):
            portfolio_page._background_refresher_loop()
        assert fake.calls == [], "数据还新不该刷新"

        # 行情旧了 (600s > 240s 周期): 第一周期即刷新
        _reset_coordinator()
        fake.calls.clear()
        _seed_quotes({"AAPL": 100.0}, age_seconds=600)
        _seed_fx("CNY", {"USD": 7.1}, age_seconds=60)
        sleeps["n"] = 0
        with pytest.raises(_StopLoop):
            portfolio_page._background_refresher_loop()
        assert _wait_for(lambda: fake.calls, sleep_fn=real_sleep), "数据旧了应刷新"
        assert _wait_coordinator_idle(real_sleep), "后台线程未跑完"

        # 汇率缺失 (行情新): 同样视为必须刷 —— 缺数据比旧数据更要补
        _reset_coordinator()
        fake.calls.clear()
        _seed_quotes({"AAPL": 100.0}, age_seconds=60)
        with sqlite3.connect(cache_mod.CACHE_DB) as con:
            con.execute("DELETE FROM fx_rates")
            con.commit()
        sleeps["n"] = 0
        with pytest.raises(_StopLoop):
            portfolio_page._background_refresher_loop()
        assert _wait_for(lambda: fake.calls, sleep_fn=real_sleep), "汇率缺失应刷新"
        assert _wait_coordinator_idle(real_sleep), "后台线程未跑完"

    def test_ensure_background_refresher_is_idempotent(self):
        """首次调用恰好起一条守护线程; 重复调用不再新增 (进程内幂等)."""

        def _count() -> int:
            return len(
                [t for t in threading.enumerate() if t.name == "quote-refresher"]
            )

        # 同一进程里前面的用例可能已启动过 (app.py main 会调), 重置标志位
        # 回到"未启动"态, 才能确定性地验证"起一条 + 不新增"
        portfolio_page._refresher_started = False
        before = _count()
        portfolio_page.ensure_background_refresher()
        portfolio_page.ensure_background_refresher()
        portfolio_page.ensure_background_refresher()
        assert _count() == before + 1
        for t in threading.enumerate():
            if t.name == "quote-refresher":
                assert t.daemon is True  # 守护线程, 不阻碍进程退出


class TestRefreshCoordinator:
    """后台刷新协调器: 去重 / 节流 / 失败兜底 / 行情+汇率一个任务."""

    Q_ARGS = (("AAPL",), False, False, False, "auto")
    F_ARGS = ("CNY", ("USD",), False)

    def _plan(self, quotes=None, fx=None):
        return {
            "quotes": quotes,
            "fx": fx,
            "dirty": bool(quotes or fx),
        }

    def test_key_changes_with_either_side(self):
        k_q = portfolio_page._plan_key(self._plan(quotes=self.Q_ARGS))
        k_q2 = portfolio_page._plan_key(self._plan(quotes=self.Q_ARGS, fx=self.F_ARGS))
        k_q3 = portfolio_page._plan_key(
            self._plan(quotes=(("MSFT",), False, False, False, "auto"))
        )
        assert len({k_q, k_q2, k_q3}) == 3

    def test_one_task_refreshes_quotes_and_fx(
        self, monkeypatch, _clean_quote_state
    ):
        q_calls, f_calls = [], []
        monkeypatch.setattr(
            prices,
            "get_quotes",
            lambda symbols, **kw: (q_calls.append(tuple(symbols)), ({}, {}, []))[1],
        )
        monkeypatch.setattr(
            portfolio_page,
            "get_fx_rates",
            lambda base, curs, **kw: (f_calls.append((base, tuple(curs))), ({}, []))[1],
        )
        plan = self._plan(quotes=self.Q_ARGS, fx=self.F_ARGS)
        key = portfolio_page._plan_key(plan)
        assert portfolio_page._start_refresh(key, plan) is True
        assert _wait_for(lambda: q_calls and f_calls), "任务未同时刷行情和汇率"
        assert q_calls == [("AAPL",)]
        assert f_calls == [("CNY", ("USD",))]

    def test_inflight_dedup_and_min_interval_throttle(
        self, monkeypatch, _clean_quote_state
    ):
        entered = threading.Event()
        release = threading.Event()

        def slow(symbols, **_kw):
            entered.set()
            release.wait(10)
            return ({}, {}, [])

        monkeypatch.setattr(prices, "get_quotes", slow)
        plan = self._plan(quotes=self.Q_ARGS)
        key = portfolio_page._plan_key(plan)

        assert portfolio_page._start_refresh(key, plan) is True
        assert entered.wait(10), "后台线程未启动"
        # 进行中: 同 key 再发起被去重 (多标签页/连续 rerun 只发一次网络)
        assert portfolio_page._start_refresh(key, plan) is False
        inflight, done = portfolio_page._refresh_state(key)
        assert inflight and done is None

        release.set()
        assert _wait_for(lambda: key in portfolio_page._refresh_done_at)
        # 刚完成: 最短重试间隔内不再发起
        assert portfolio_page._start_refresh(key, plan) is False
        # 完成时间拨到间隔之外 → 允许再次发起
        with portfolio_page._refresh_lock:
            portfolio_page._refresh_done_at[key] = (
                time.time() - portfolio_page._REFRESH_MIN_INTERVAL - 1
            )
        assert portfolio_page._start_refresh(key, plan) is True
        assert _wait_for(lambda: not portfolio_page._refresh_state(key)[0])

    def test_failure_swallowed_and_recorded(
        self, monkeypatch, _clean_quote_state
    ):
        """刷新失败不冒泡到页面 (陈旧数据继续展示), 但记完成时间供看门狗节流."""

        def boom(symbols, **_kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(prices, "get_quotes", boom)
        plan = self._plan(quotes=self.Q_ARGS)
        key = portfolio_page._plan_key(plan)
        assert portfolio_page._start_refresh(key, plan) is True
        assert _wait_for(lambda: key in portfolio_page._refresh_done_at)
        inflight, _done = portfolio_page._refresh_state(key)
        assert not inflight
