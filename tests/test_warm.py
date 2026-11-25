"""无 UI 依赖的缓存保温/预热服务 + CLI warm 子命令测试."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tracker.prices import Quote
from tracker.services import warm as warm_mod


@pytest.fixture(autouse=True)
def _skip_auto_cache_prune(monkeypatch):
    """默认跳过 warm_once 的每日孤儿缓存清理, 避免测试互相影响."""
    monkeypatch.setattr(warm_mod, "_last_cache_prune", warm_mod.time.time())


def test_warm_service_has_no_streamlit_dependency():
    """CLI 只装核心依赖也要能保温: 服务模块不得间接 import streamlit."""
    code = (
        "import sys; import tracker.services.warm; "
        "assert 'streamlit' not in sys.modules"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def _settings():
    return {
        "prefer_akshare": False,
        "use_ibkr": False,
        "use_longport": False,
        "crypto_source": "auto",
    }


def test_disk_refresh_plan_accepts_extra_symbols(monkeypatch):
    """CLI --symbols 可扩展预热范围; 代码归一为 Yahoo 规范形态."""
    monkeypatch.setattr(warm_mod.storage, "load_settings", _settings)
    monkeypatch.setattr(
        warm_mod.storage, "load_portfolio", lambda *a, **k: {"holdings": []}
    )
    monkeypatch.setattr(
        warm_mod.storage, "load_watchlist", lambda *a, **k: {"watchlist": []}
    )
    plan = warm_mod.disk_refresh_plan(extra_symbols=["AAPL", "600519.SH"])
    assert plan is not None
    assert plan["quotes"][0] == ("AAPL", "600519.SS")


def test_warm_quotes_once_derives_fx_after_cold_start(monkeypatch):
    """冷启动没有行情缓存时, 也要按刚取回的币种补汇率."""
    monkeypatch.setattr(warm_mod.storage, "load_settings", _settings)
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {
            "base_currency": "CNY",
            "holdings": [{"symbol": "AAPL"}],
        },
    )
    monkeypatch.setattr(
        warm_mod.storage, "load_watchlist", lambda *a, **k: {"watchlist": []}
    )
    quote = Quote(
        symbol="AAPL",
        name="Apple",
        price=200.0,
        prev_close=198.0,
        change_pct=1.0,
        currency="USD",
    )
    monkeypatch.setattr(
        warm_mod.prices,
        "get_quotes",
        lambda symbols, **kw: ({"AAPL": quote}, {}, []),
    )
    calls: list[tuple] = []

    def fake_fx(base, curs, **kw):
        calls.append((base, tuple(curs)))
        return {"USD": 7.1}, []

    monkeypatch.setattr(warm_mod, "get_fx_rates", fake_fx)
    quotes, fx = warm_mod.warm_quotes_once()
    assert quotes["ok"] == 1
    assert quotes["symbols"] == ["AAPL"]
    assert calls == [("CNY", ("USD",))]
    assert fx == {
        "base": "CNY",
        "currencies": ["USD"],
        "ok": 1,
        "missing": [],
    }


def test_warm_calendar_once_uses_default_window(monkeypatch):
    """日历预热沿用页面默认窗口: 本月+下月, 不开长桥兜底."""
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {"holdings": [{"symbol": "AAPL"}]},
    )
    monkeypatch.setattr(
        warm_mod.storage, "load_watchlist", lambda *a, **k: {"watchlist": []}
    )
    seen: list[dict] = []

    def fake_fetch(symbols, **kw):
        seen.append({"symbols": symbols, **kw})
        return {
            "events": [{"symbol": "AAPL", "date": "2026-11-20"}],
            "cache": {"used": True, "stale": False},
            "issues": [],
        }

    monkeypatch.setattr(warm_mod.events_mod, "fetch_calendar", fake_fetch)
    out = warm_mod.warm_calendar_once()
    assert out is not None
    assert out["months"] == 2
    assert out["use_longport"] is False
    assert out["events"] == 1
    assert seen[0]["months"] == 2
    assert seen[0]["refresh"] is False


def test_warm_once_can_skip_calendar(monkeypatch):
    monkeypatch.setattr(
        warm_mod,
        "warm_quotes_once",
        lambda **kw: ({"symbols": [], "ok": 0, "errors": {}, "notes": []}, None),
    )
    monkeypatch.setattr(
        warm_mod,
        "warm_calendar_once",
        lambda **kw: {"symbols": [], "months": 2, "events": 0},
    )
    result = warm_mod.warm_once(include_calendar=False)
    assert "quotes" in result
    assert "calendar" not in result


def test_warm_holdings_history_only_portfolio(monkeypatch):
    """K线保温只覆盖持仓 (+--symbols), 不把 702 只自选全拉一遍."""
    pd = pytest.importorskip("pandas")
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {
            "holdings": [
                {"symbol": "AAPL"},
                {"symbol": "600519.SS"},
                {"symbol": "AAPL"},  # 去重
                {"symbol": None},
            ]
        },
    )
    monkeypatch.setattr(warm_mod.storage, "load_settings", _settings)
    calls: list[dict] = []

    def fake_ohlc(sym, **kw):
        calls.append({"symbol": sym, **kw})
        return pd.DataFrame({"date": ["2026-01-01"], "close": [1.0]})

    monkeypatch.setattr(warm_mod.prices, "get_ohlc", fake_ohlc)
    out = warm_mod.warm_holdings_history_once(extra_symbols=["MSFT"])
    assert out["skipped"] is False
    assert out["ok"] == 3
    assert out["errors"] == {}
    assert [c["symbol"] for c in calls] == ["AAPL", "600519.SS", "MSFT"]
    assert {c["months"] for c in calls} == {warm_mod.HISTORY_WARM_MONTHS}
    assert {c["cache_ttl"] for c in calls} == {warm_mod.HISTORY_WARM_TTL}


def test_warm_holdings_history_collects_errors(monkeypatch):
    pd = pytest.importorskip("pandas")
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {"holdings": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]},
    )
    monkeypatch.setattr(warm_mod.storage, "load_settings", _settings)
    monkeypatch.setattr(
        warm_mod.prices,
        "get_ohlc",
        lambda sym, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        if sym == "MSFT"
        else pd.DataFrame({"date": ["2026-01-01"], "close": [1.0]}),
    )
    out = warm_mod.warm_holdings_history_once()
    assert out["ok"] == 1
    assert out["errors"] == {"MSFT": "boom"}


def test_warm_valuation_only_stocks(monkeypatch):
    """估值预热覆盖持仓 + PB/市值自选, 纯价格自选/crypto/index 不参与."""
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {
            "holdings": [
                {"symbol": "AAPL"},
                {"symbol": "600519.SS"},
                {"symbol": "BTC-USD"},
                {"symbol": "IX.DXY"},
                {"symbol": "AAPL"},
            ]
        },
    )
    monkeypatch.setattr(
        warm_mod.storage,
        "load_watchlist",
        lambda *a, **k: {
            "watchlist": [
                {"symbol": "TSLA", "metric": "pb"},
                {"symbol": "NVDA"},  # 纯价格自选: 不预取估值
                {"symbol": "BABA", "mcap": 1.0e12},
            ]
        },
    )
    seen: list[str] = []
    monkeypatch.setattr(
        "tracker.valuation.fetch_snapshot",
        lambda sym, **kw: seen.append(sym) or object(),
    )
    out = warm_mod.warm_valuation_once(extra_symbols=["MSFT"])
    assert out["ok"] == 5
    assert out["errors"] == {}
    assert sorted(seen) == ["600519.SS", "AAPL", "BABA", "MSFT", "TSLA"]


def test_warm_holdings_valuation_collects_errors(monkeypatch):
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {"holdings": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]},
    )
    monkeypatch.setattr(
        "tracker.valuation.fetch_snapshot",
        lambda sym, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        if sym == "MSFT"
        else object(),
    )
    out = warm_mod.warm_valuation_once()
    assert out["ok"] == 1
    assert out["errors"] == {"MSFT": "boom"}


def test_warm_once_includes_history_and_flushes_health(monkeypatch):
    monkeypatch.setattr(
        warm_mod,
        "warm_quotes_once",
        lambda **kw: ({"symbols": [], "ok": 0, "errors": {}, "notes": []}, None),
    )
    monkeypatch.setattr(
        warm_mod,
        "warm_holdings_history_once",
        lambda **kw: {"symbols": ["AAPL"], "ok": 1, "errors": {}, "skipped": False},
    )
    monkeypatch.setattr(
        warm_mod,
        "warm_valuation_once",
        lambda **kw: {"symbols": ["AAPL"], "ok": 1, "errors": {}, "skipped": False},
    )
    monkeypatch.setattr(warm_mod, "warm_calendar_once", lambda **kw: None)
    calls: list[bool] = []
    import tracker.health as health_mod

    monkeypatch.setattr(health_mod, "flush", lambda: calls.append(True))
    result = warm_mod.warm_once()
    assert result["history"]["ok"] == 1
    assert result["valuation"]["ok"] == 1
    assert calls == [True]


def test_warm_once_prunes_orphans_at_most_once_per_day(monkeypatch):
    monkeypatch.setattr(warm_mod, "_last_cache_prune", 0.0)
    monkeypatch.setattr(
        warm_mod,
        "warm_quotes_once",
        lambda **kw: ({"symbols": [], "ok": 0, "errors": {}, "notes": []}, None),
    )
    monkeypatch.setattr(
        warm_mod, "warm_holdings_history_once", lambda **kw: {"skipped": True}
    )
    monkeypatch.setattr(warm_mod, "warm_valuation_once", lambda **kw: {"skipped": True})
    monkeypatch.setattr(warm_mod, "warm_calendar_once", lambda **kw: None)
    calls: list[int] = []
    monkeypatch.setattr(
        "tracker.cache.prune_orphans",
        lambda **kw: calls.append(1) or {"deleted": 0},
    )
    warm_mod.warm_once()
    warm_mod.warm_once()
    assert calls == [1]


def test_refresh_period_scales_with_batch_quotes():
    """行情已批量: 706 只的默认周期应与 300s 缓存新鲜期对齐, 不再 23 分钟."""
    assert warm_mod.refresh_period(706) == warm_mod.REFRESH_PERIOD
    assert warm_mod.refresh_period(5000) == warm_mod.REFRESH_PERIOD_MAX


def test_cli_warm_once_json(monkeypatch, capsys):
    from tracker.cli import build_parser

    result = {
        "quotes": {
            "symbols": ["AAPL"],
            "ok": 1,
            "errors": {},
            "notes": [],
            "skipped": False,
        },
        "fx": {"base": "CNY", "currencies": ["USD"], "ok": 1, "missing": []},
        "calendar": None,
        "elapsed_seconds": 0.1,
    }
    monkeypatch.setattr(warm_mod, "warm_once", lambda **kw: result)
    args = build_parser().parse_args(["warm", "--json"])
    args.func(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["quotes"]["ok"] == 1


def test_cli_warm_once_text(monkeypatch, capsys):
    from tracker.cli import build_parser

    result = {
        "quotes": {
            "symbols": ["AAPL"],
            "ok": 1,
            "errors": {},
            "notes": [],
            "skipped": False,
        },
        "fx": None,
        "history": {
            "symbols": ["AAPL"],
            "ok": 1,
            "errors": {},
            "skipped": False,
        },
        "valuation": {
            "symbols": ["AAPL"],
            "ok": 1,
            "errors": {},
            "skipped": False,
        },
        "calendar": {"months": 2, "events": 3, "issues": []},
        "elapsed_seconds": 0.1,
    }
    monkeypatch.setattr(warm_mod, "warm_once", lambda **kw: result)
    args = build_parser().parse_args(["warm"])
    args.func(args)
    out = capsys.readouterr().out
    assert "=== 缓存预热 ===" in out
    assert "行情: 1/1 只成功" in out
    assert "K线: 1/1 只成功" in out
    assert "估值: 1/1 只成功" in out
    assert "日历: 2 个月窗口, 3 个事件" in out


# ---------- 共享保温单例 (var/warm.lock) ----------


def test_shared_warm_lock_is_cross_process(monkeypatch, tmp_path):
    """一个进程持锁时, 另一个进程不能再成为常驻保温; 释放后可接管."""
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "TRACKER_HOME": str(tmp_path)}
    code = (
        "from tracker.services import warm; "
        "print(warm.acquire_warm_leader('child'))"
    )

    def child() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    warm_mod.release_warm_leader()
    try:
        assert warm_mod.acquire_warm_leader("parent") is True
        assert warm_mod.warm_lock_path() == tmp_path / "var" / "warm.lock"
        # 同进程重复获取幂等
        assert warm_mod.acquire_warm_leader("again") is True

        blocked = child()
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "False"
    finally:
        warm_mod.release_warm_leader()

    # leader 退出 (锁 fd 关闭) 后, 下一进程可以接管
    took_over = child()
    assert took_over.returncode == 0, took_over.stderr
    assert took_over.stdout.strip() == "True"


def test_ensure_warmers_respects_shared_lock(monkeypatch):
    """Streamlit 入口只在成为 leader 时起线程, 否则完全让位给常驻进程."""
    calls: list[str] = []
    monkeypatch.setattr(
        warm_mod, "ensure_background_refresher", lambda: calls.append("quotes")
    )
    monkeypatch.setattr(
        warm_mod, "ensure_calendar_refresher", lambda: calls.append("calendar")
    )

    monkeypatch.setattr(
        warm_mod, "acquire_warm_leader", lambda role="warm": False
    )
    warm_mod.ensure_warmers()
    assert calls == []

    monkeypatch.setattr(
        warm_mod, "acquire_warm_leader", lambda role="warm": True
    )
    warm_mod.ensure_warmers()
    assert calls == ["quotes", "calendar"]


def test_cli_resident_waits_for_existing_leader(monkeypatch, capsys):
    """已有常驻保温时, 第二个 `warm --interval` 不重复刷新, 原地等待接管."""
    from tracker.cli import build_parser

    monkeypatch.setattr(
        warm_mod, "acquire_warm_leader", lambda role="warm": False
    )
    monkeypatch.setattr(
        warm_mod,
        "warm_once",
        lambda **kw: pytest.fail("等待 leader 期间不应发起预热"),
    )

    class _StopSleep:
        def sleep(self, _seconds):
            raise KeyboardInterrupt

    monkeypatch.setattr("tracker.cli.warm.time", _StopSleep())
    args = build_parser().parse_args(["warm", "--interval", "300"])
    args.func(args)

    out = capsys.readouterr().out
    assert "等待其退出后接管" in out
    assert "已停止保温" in out


def test_status_reports_shared_leader(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
    warm_mod.release_warm_leader()

    before = warm_mod.status()["leader"]
    assert before == {"held": False, "role": None, "external": False}

    assert warm_mod.acquire_warm_leader("unit") is True
    try:
        leader = warm_mod.status()["leader"]
        assert leader["held"] is True
        assert leader["role"] == "unit"
        assert leader["external"] is False
    finally:
        warm_mod.release_warm_leader()
