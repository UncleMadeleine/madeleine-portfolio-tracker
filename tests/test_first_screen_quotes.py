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


def _wait_for(pred, timeout: float = 10.0) -> bool:
    """轮询等待后台条件 (测试里替代 sleep 定长)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


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
        """汇率陈旧 (行情全新): 旧汇率秒算市值, 后台只刷汇率, 看门狗 rerun 换新."""
        _seed_quotes({"AAPL": 100.0}, age_seconds=30)  # 行情全新
        _seed_fx("CNY", {"USD": 5.0}, age_seconds=600)  # 汇率陈旧
        fake_fx = _recording_fx(7.1)

        def boom_quotes(symbols, **_kw):
            raise AssertionError("行情全新不应触发取数")

        at = _boot(
            monkeypatch, tmp_path, get_quotes=boom_quotes, get_fx=fake_fx
        )

        # 旧汇率 2 × 100 × 5.0 = 1,000 (若阻塞等刷新会是 1,420)
        assert any("1,000" in str(m.value) for m in at.metric)
        assert _plan_dirty(at)

        # 后台只刷汇率 (不动行情), 落库后看门狗 rerun → 新汇率 1,420
        assert _wait_for(lambda: fake_fx.calls), "后台汇率刷新未被触发"
        rates, _ = cache_mod.get_fx_cached("CNY", ["USD"])
        assert rates["USD"] == 7.1
        at.run(timeout=60)
        assert any("1,420" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)

    def test_cold_start_blocks_and_fetches(
        self, monkeypatch, tmp_path, _clean_quote_state
    ):
        """空磁盘 (冷启动): 没有可展示数据, 保持阻塞拉取, 不挂后台任务."""
        fake = _recording_quotes(150.0)
        at = _boot(monkeypatch, tmp_path, get_quotes=fake)
        assert fake.calls  # 同步走完 cached_quotes
        assert any("2,130" in str(m.value) for m in at.metric)
        assert not _plan_dirty(at)

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
