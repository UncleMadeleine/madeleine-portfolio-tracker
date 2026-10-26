"""财经日历域测试: 纯函数 + 缓存 + 源链 (全部离线, 网络函数 monkeypatch)."""

from datetime import date

import pytest

from tracker import events
from tracker.events import (
    _cn_code_to_yahoo,
    _cn_normalize_disclosure,
    _month_starts,
    _pick_cn_scheduled_row,
    _window_rows,
    _yf_calendar_rows,
    cn_periods_for_window,
    fetch_calendar,
    normalize_longport_event,
    window_from_months,
)


@pytest.fixture
def no_network_calendar(monkeypatch):
    """断网兜底双保险: events 的网络入口全部 patch 成可控假源.

    连 akshare 的两个整批函数一起 patch —— cn 缓存 (_fetch_cn_period) 与单票
    g: 缓存的真实读写路径保留可测, 网络函数被替换。
    """
    import pandas as pd

    calls = {"global": [], "cn": []}

    def fake_yf_rows(symbol):
        calls["global"].append(symbol)
        if symbol == "AAPL":
            return [
                {
                    "type": "earnings",
                    "date": date(2026, 11, 3),
                    "date_status": "estimate",
                    "note": "EPS 估计 ~1.98",
                }
            ]
        return []

    cn_df = pd.DataFrame(
        [
            {
                "序号": 1,
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "首次预约时间": "2026-10-31",
                "一次变更日期": "NaT",
                "二次变更日期": "NaT",
                "三次变更日期": "NaT",
                "实际披露时间": "NaT",
            }
        ]
    )
    empty_df = pd.DataFrame()

    def fake_yysj(symbol, date):
        calls["cn"].append((symbol, date))
        # 沪深A股 → 茅台行; 京市A股/无此期 → 空表 (与真实接口同构)
        return cn_df if symbol == "沪深A股" and date == "20260930" else empty_df

    def fake_fhps(date):
        calls["cn"].append(("fhps", date))
        return empty_df

    monkeypatch.setattr(events, "_yf_calendar_rows", fake_yf_rows)
    monkeypatch.setattr("akshare.stock_yysj_em", fake_yysj)
    monkeypatch.setattr("akshare.stock_fhps_em", fake_fhps)
    return calls


# ---------------------------------------------------------------------------
# 窗口 / 报表期 (纯函数)
# ---------------------------------------------------------------------------


class TestWindow:
    def test_two_months_current_plus_next(self):
        s, e = window_from_months(2, date(2026, 10, 22))
        assert (s, e) == (date(2026, 10, 1), date(2026, 11, 30))

    def test_one_month_end_of_year(self):
        s, e = window_from_months(1, date(2026, 12, 3))
        assert (s, e) == (date(2026, 12, 1), date(2026, 12, 31))

    def test_three_months(self):
        s, e = window_from_months(3, date(2026, 11, 1))
        assert (s, e) == (date(2026, 11, 1), date(2027, 1, 31))


class TestCnPeriods:
    def test_oct_window_looks_back_two_quarters(self):
        ps = cn_periods_for_window(date(2026, 10, 1), date(2026, 11, 30))
        assert ps == ["20260630", "20260930"]

    def test_no_future_period_beyond_window(self):
        # 年报期 (20261231) 披露必在其后, 窗口 Oct-Nov 不应包含
        ps = cn_periods_for_window(date(2026, 10, 1), date(2026, 11, 30))
        assert "20261231" not in ps

    def test_january_window_includes_current_year_end(self):
        ps = cn_periods_for_window(date(2027, 1, 1), date(2027, 2, 28))
        assert "20261231" in ps


class TestMonthSlices:
    def test_two_months(self):
        segs = _month_starts(date(2026, 10, 22), date(2026, 12, 15))
        assert segs[0] == (date(2026, 10, 22), date(2026, 10, 31))
        assert segs[1] == (date(2026, 11, 1), date(2026, 11, 30))
        assert segs[2] == (date(2026, 12, 1), date(2026, 12, 15))


# ---------------------------------------------------------------------------
# cn 域归一化
# ---------------------------------------------------------------------------


def _make_series(**kwargs):
    import pandas as pd

    return pd.Series({"股票代码": None, **kwargs})


class TestCnCode:
    def test_sh(self):
        assert _cn_code_to_yahoo("600519") == "600519.SS"

    def test_sz(self):
        assert _cn_code_to_yahoo("000001") == "000001.SZ"
        assert _cn_code_to_yahoo("300001") == "300001.SZ"

    def test_bj(self):
        assert _cn_code_to_yahoo("830799") == "830799.BJ"
        assert _cn_code_to_yahoo("430047") == "430047.BJ"
        assert _cn_code_to_yahoo("920002") == "920002.BJ"

    def test_b_shares(self):
        assert _cn_code_to_yahoo("900902") == "900902.SS"
        assert _cn_code_to_yahoo("200012") == "200012.SZ"

    def test_non_code(self):
        assert _cn_code_to_yahoo("abc") is None
        assert _cn_code_to_yahoo("") is None
        assert _cn_code_to_yahoo("12345") is None


class TestScheduledRow:
    def test_first_scheduled_only(self):
        d, status, chain = _pick_cn_scheduled_row(
            _make_series(
                首次预约时间="2026-10-31", 实际披露时间="NaT"
            )
        )
        assert (d, status, chain) == (date(2026, 10, 31), "planned", 0)

    def test_change_overrides_first(self):
        d, status, chain = _pick_cn_scheduled_row(
            _make_series(
                首次预约时间="2026-10-31",
                一次变更日期="2026-11-03",
                实际披露时间="NaT",
            )
        )
        assert (d, status, chain) == (date(2026, 11, 3), "changed", 1)

    def test_actual_wins(self):
        d, status, chain = _pick_cn_scheduled_row(
            _make_series(
                首次预约时间="2026-10-31",
                实际披露时间="2026-10-28",
            )
        )
        assert (d, status) == (date(2026, 10, 28), "actual")


class TestDiscloseNormalize:
    def test_rows_normalized(self):
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "600519",
                    "股票简称": "贵州茅台",
                    "首次预约时间": "2026-10-31",
                    "一次变更日期": "NaT",
                    "二次变更日期": "NaT",
                    "三次变更日期": "NaT",
                    "实际披露时间": "NaT",
                },
                {
                    "序号": 2,
                    "股票代码": "999999",  # 非法形态跳过
                    "股票简称": "幻影",
                    "首次预约时间": "2026-10-31",
                    "实际披露时间": "NaT",
                },
            ]
        )
        evs = _cn_normalize_disclosure(df, "20260930")
        assert len(evs) == 1
        assert evs[0]["symbol"] == "600519.SS"
        assert evs[0]["name"] == "贵州茅台"
        assert evs[0]["date"] == "2026-10-31"
        assert evs[0]["date_status"] == "planned"

    def test_nat_dates_dropped(self):
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "股票代码": "600519",
                    "股票简称": "x",
                    "首次预约时间": pd.NaT,
                    "实际披露时间": pd.NaT,
                }
            ]
        )
        assert _cn_normalize_disclosure(df, "20260930") == []


# ---------------------------------------------------------------------------
# global 域: yfinance calendar 行 → 窗口
# ---------------------------------------------------------------------------


class TestYfRowsAndWindow:
    def _cal_rows(self, monkeypatch, cal):
        monkeypatch.setattr(
            "yfinance.Ticker", lambda symbol: type("T", (), {"calendar": cal})
        )
        return _yf_calendar_rows("AAPL")

    def test_parse_dates_list(self, monkeypatch):
        rows = self._cal_rows(
            monkeypatch,
            {
                "Earnings Date": [date(2026, 11, 3)],
                "Earnings Average": 1.9819,
                "Ex-Dividend Date": "2026-08-10",
            },
        )
        types = {r["type"]: r for r in rows}
        assert types["earnings"]["date"] == date(2026, 11, 3)
        assert "EPS 估计 ~1.98" in types["earnings"]["note"]
        assert types["ex_dividend"]["date"] == date(2026, 8, 10)

    def test_empty_calendar(self, monkeypatch):
        rows = self._cal_rows(monkeypatch, None)
        assert rows == []
        rows = self._cal_rows(monkeypatch, {})
        assert rows == []

    def test_window_filter(self):
        evs = [
            {"type": "earnings", "date": date(2026, 11, 3), "source": "yfinance"},
            {"type": "earnings", "date": date(2025, 1, 1), "source": "yfinance"},
        ]
        picked = _window_rows(
            evs, date(2026, 10, 1), date(2026, 11, 30), source="yfinance"
        )
        assert len(picked) == 1
        assert picked[0].get("symbol") is None  # _window_rows 不注 symbol (调用方注)
        assert picked[0]["date"] == date(2026, 11, 3)


# ---------------------------------------------------------------------------
# longport 归一化 / 市场轴
# ---------------------------------------------------------------------------


class TestLongportNormalize:
    class _Kv:
        def __init__(self, k, v):
            self.key = k
            self.value = v

    class _Info:
        symbol = "0700.HK"
        content = "Q3 业绩"
        counter_name = "腾讯控股"
        date = "2026.11.18"
        data_kv = None
        star = 2

    def test_maps_and_fields(self):
        ev = normalize_longport_event(self._Info(), "Report")
        assert ev["symbol"] == "0700.HK"
        assert ev["date"] == "2026-11-18"
        assert ev["type"] == "earnings"
        assert ev["note"] == "Q3 业绩; ★★"

    def test_unmapped_symbol(self):
        class Info(self._Info):
            symbol = "30.5.10"

        assert normalize_longport_event(Info(), "Dividend") is None

    def test_market_axis(self):
        assert events._lp_market_axis("AAPL") == "US"
        assert events._lp_market_axis("0700.HK") == "HK"
        assert events._lp_market_axis("600519.SS") == "SH"
        assert events._lp_market_axis("000001.SZ") == "SZ"
        assert events._lp_market_axis("SAP.DE") is None


# ---------------------------------------------------------------------------
# 顶层面板 fetch_calendar (离线假源)
# ---------------------------------------------------------------------------


class TestFetchCalendar:
    def test_global_and_cn_merge(self, no_network_calendar):
        r = fetch_calendar(
            ["AAPL", "600519.SS"], months=2, names={"AAPL": "苹果公司"}
        )
        syms = {e["symbol"] for e in r["events"]}
        assert {"AAPL", "600519.SS"} <= syms
        by = {e["symbol"]: e for e in r["events"]}
        assert by["AAPL"]["date"] == "2026-11-03"
        assert by["AAPL"]["source"] == "yfinance"
        assert by["AAPL"]["name"] == "苹果公司"  # names 参数补齐
        assert by["600519.SS"]["source"] == "akshare"
        assert by["600519.SS"]["name"] == "贵州茅台"
        assert r["window"]["start"] == "2026-10-01"
        assert r["sources"] == {"yfinance": 1, "akshare": 1}

    def test_skips_crypto_index(self, no_network_calendar):
        r = fetch_calendar(["AAPL", "BTC-USD", "IX.DXY"], months=2)
        assert {e["symbol"] for e in r["events"]} == {"AAPL"}

    def test_uses_cn_cache_second_call(self, no_network_calendar, monkeypatch):
        fetch_calendar(["600519.SS"], months=2)
        n_first = len(no_network_calendar["cn"])
        assert n_first >= 2  # 沪深/koi/fhps 两报表期至少出过网络调用
        # 第二次: cn 整批缓存命中, 网络函数 (akshare) 不再被调
        monkeypatch.setattr(
            "akshare.stock_yysj_em",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        monkeypatch.setattr(
            "akshare.stock_fhps_em",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        r = fetch_calendar(["600519.SS"], months=2)
        assert len(no_network_calendar["cn"]) == n_first
        assert any(e["symbol"] == "600519.SS" for e in r["events"])

    def test_global_negative_cache(self, no_network_calendar, monkeypatch):
        monkeypatch.setattr(events, "_YF_PAUSE", 0)
        # 未收录符号: 空载荷落库 (负缓存), 第二次不再走 Hugh 网络 patch
        fetch_calendar(["UNKNOWN_OK"], months=2)
        n_first = len(no_network_calendar["global"])
        monkeypatch.setattr(
            events,
            "_yf_calendar_rows",
            lambda s: (_ for _ in ()).throw(AssertionError("negative cache miss")),
        )
        r = fetch_calendar(["UNKNOWN_OK"], months=2)
        assert r["events"] == []
        assert len(no_network_calendar["global"]) == n_first


class TestLongportFallback:
    def test_global_fallback_when_no_events(self, no_network_calendar, monkeypatch):
        lp_calls = {}

        def fake_lp(symbols, start, end, cfg=None, refresh=False):
            lp_calls["syms"] = sorted(symbols)
            return {"ZZZ-US": []}, None

        monkeypatch.setattr(events, "_fetch_longport_events", fake_lp)
        stored_yf = events._yf_calendar_rows
        monkeypatch.setattr(events, "_yf_calendar_rows", lambda s: [])

        r = fetch_calendar(["NOEARNINGS"], months=2, use_longport=True)
        # 只对 US/HK 可兜底的代码调用长桥
        assert lp_calls["syms"] == ["NOEARNINGS"]
        assert r["events"] == []
        # use_longport 关闭时不调兜底
        lp_calls.clear()
        monkeypatch.setattr(events, "_fetch_longport_events", fake_lp)
        r2 = fetch_calendar(["NOEARNINGS"], months=2, use_longport=False)
        assert "syms" not in lp_calls
        del stored_yf, r, r2


# ---------------------------------------------------------------------------
# 杂项: 完整 fetch → json 序列化 (CLI --json 路径)
# ---------------------------------------------------------------------------


def test_json_serializable(no_network_calendar):
    import json

    r = events.fetch_calendar(["AAPL", "600519.SS"], months=2)
    json.dumps(r, ensure_ascii=False)  # 不抛 TypeError 即通过


def test_storage_paths_untouched():
    # 数据目录隔离由 conftest 保证; 这里确认常量指向隔离路径 (防误写真实数据)
    from tracker.storage import PORTFOLIO_PATH

    assert "tracker-home" in str(PORTFOLIO_PATH) or "tmp" in str(PORTFOLIO_PATH)


# ---------------------------------------------------------------------------
# 整批缓存 TTL / 失败不落负缓存 (回归: 整批缓存曾无 TTL, 一次失败被固化 6h)
# ---------------------------------------------------------------------------


def _age_calendar_row(key: str, age_seconds: float) -> None:
    """把日历缓存行的 fetched_at 改成 age_seconds 之前 (模拟过期)."""
    import sqlite3
    import time

    from tracker import cache as cache_mod

    with sqlite3.connect(cache_mod.CACHE_DB) as con:
        cur = con.execute(
            "UPDATE calendar_cache SET fetched_at=? WHERE key=?",
            (time.time() - age_seconds, key),
        )
        assert cur.rowcount == 1, f"缓存行不存在: {key}"
        con.commit()


class TestBatchCacheTTL:
    def test_cn_period_cache_expires_after_ttl(
        self, monkeypatch, no_network_calendar
    ):
        """cn 整批缓存超过 6h 必须回源, 不能永久命中旧表."""
        from tracker import cache as cache_mod

        cache_mod.set_calendar_row(
            "cn:20260930",
            [{"symbol": "600519.SS", "type": "earnings", "date": "2026-10-31"}],
        )
        _age_calendar_row("cn:20260930", events.CAL_TTL + 1)
        before = len(no_network_calendar["cn"])
        data, issues = events._fetch_cn_period("20260930", refresh=False)
        assert len(no_network_calendar["cn"]) > before, "缓存过期后仍命中旧表"
        assert issues == []
        assert any(e["symbol"] == "600519.SS" for e in data["events"])

    def test_cn_period_fresh_cache_hits(self, no_network_calendar):
        """TTL 内的整批缓存仍应零请求命中 (回归不把 TTL 做成永远回源)."""
        from tracker import cache as cache_mod

        cache_mod.set_calendar_row(
            "cn:20260930",
            [{"symbol": "600519.SS", "type": "earnings", "date": "2026-10-31"}],
        )
        before = len(no_network_calendar["cn"])
        data, issues = events._fetch_cn_period("20260930", refresh=False)
        assert len(no_network_calendar["cn"]) == before
        assert issues == []
        assert data["events"][0]["symbol"] == "600519.SS"

    def test_cn_period_all_sources_failed_not_cached(self, monkeypatch):
        """akshare 全部失败时不得把空结果写进缓存 (否则故障被固化 6h)."""

        def boom(*a, **kw):
            raise OSError("offline")

        monkeypatch.setattr("akshare.stock_yysj_em", boom)
        monkeypatch.setattr("akshare.stock_fhps_em", boom)
        from tracker import cache as cache_mod

        data, issues = events._fetch_cn_period("20261231", refresh=False)
        assert data["events"] == []
        assert len(issues) == 3  # 沪深/京市/分红三条失败注记
        assert cache_mod.get_calendar_row("cn:20261231") is None
        # 第二次调用必须重试而不是命中空负缓存
        _, issues2 = events._fetch_cn_period("20261231", refresh=False)
        assert len(issues2) == 3


class _LpInfo:
    def __init__(self, symbol: str, d: str):
        self.symbol = symbol
        self.date = d
        self.content = ""
        self.counter_name = ""
        self.data_kv = []
        self.star = 0


class _LpGroup:
    def __init__(self, infos):
        self.infos = infos


class _LpResp:
    def __init__(self, infos, next_date=""):
        self.list = [_LpGroup(infos)]
        self.next_date = next_date


class _LpApi:
    def __init__(self, infos, next_date=""):
        self._infos = infos
        self._next = next_date
        self.calls = 0

    def finance_calendar(self, category, cursor, end, market):
        self.calls += 1
        return _LpResp(self._infos, self._next)


class TestLongportBatchCache:
    _START = date(2026, 10, 1)
    _END = date(2026, 10, 31)
    _KEY = "lp:US:Report:2026-10"

    def test_lp_month_cache_expires_after_ttl(self, monkeypatch):
        """长桥整批缓存同样受 6h TTL 约束."""
        from tracker import cache as cache_mod

        cache_mod.set_calendar_row(
            self._KEY,
            [{"symbol": "AAPL", "type": "earnings", "date": "2026-10-09"}],
        )
        _age_calendar_row(self._KEY, events.CAL_TTL + 1)
        api = _LpApi([_LpInfo("MSFT.US", "2026.10.20")])
        monkeypatch.setattr(events, "_lp_config_builder", lambda cfg=None: api)
        evs, err = events._fetch_lp_month_events(
            "US", "Report", self._START, self._END
        )
        assert err is None
        assert api.calls == 1
        assert [e["symbol"] for e in evs] == ["MSFT"]

    def test_lp_refresh_bypasses_cache(self, monkeypatch):
        """--refresh 必须穿透长桥整批缓存 (此前参数根本没有传进来)."""
        from tracker import cache as cache_mod

        cache_mod.set_calendar_row(
            self._KEY,
            [{"symbol": "AAPL", "type": "earnings", "date": "2026-10-09"}],
        )
        api = _LpApi([_LpInfo("MSFT.US", "2026.10.20")])
        monkeypatch.setattr(events, "_lp_config_builder", lambda cfg=None: api)
        evs, err = events._fetch_lp_month_events(
            "US", "Report", self._START, self._END, refresh=True
        )
        assert err is None
        assert api.calls == 1
        assert [e["symbol"] for e in evs] == ["MSFT"]

    def test_lp_pagination_stops_when_cursor_stuck(self, monkeypatch):
        """next_date 不前进时必须收尾, 不能死循环."""
        api = _LpApi([], next_date="2026-10-01")
        monkeypatch.setattr(events, "_lp_config_builder", lambda cfg=None: api)
        evs, err = events._fetch_lp_month_events(
            "US", "Report", self._START, self._END, refresh=True
        )
        assert (evs, err) == ([], None)
        assert api.calls == 1
