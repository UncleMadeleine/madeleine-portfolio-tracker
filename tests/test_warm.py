"""无 UI 依赖的缓存保温/预热服务 + CLI warm 子命令测试."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tracker.prices import Quote
from tracker.services import warm as warm_mod


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
        "calendar": {"months": 2, "events": 3, "issues": []},
        "elapsed_seconds": 0.1,
    }
    monkeypatch.setattr(warm_mod, "warm_once", lambda **kw: result)
    args = build_parser().parse_args(["warm"])
    args.func(args)
    out = capsys.readouterr().out
    assert "=== 缓存预热 ===" in out
    assert "行情: 1/1 只成功" in out
    assert "日历: 2 个月窗口, 3 个事件" in out
