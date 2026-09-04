import pandas as pd

import tracker.fx as fx


def no_cfets(monkeypatch):
    monkeypatch.setattr(fx, "_cfets_rate", lambda s, d: None)


def test_get_rate_identity():
    assert fx.get_rate("USD", "USD") == 1.0


def test_cfets_first(monkeypatch):
    monkeypatch.setattr(fx, "_cfets_rate", lambda s, d: 7.1)
    monkeypatch.setattr(fx, "_yahoo_pair", lambda p: 7.2)
    assert fx.get_rate("USD", "CNY") == 7.1


def test_yahoo_direct_pair(monkeypatch):
    no_cfets(monkeypatch)
    monkeypatch.setattr(fx, "_yahoo_pair", lambda p: 7.2 if p == "USDCNY=X" else None)
    assert fx.get_rate("USD", "CNY") == 7.2


def test_yahoo_inverse_pair(monkeypatch):
    no_cfets(monkeypatch)
    monkeypatch.setattr(fx, "_yahoo_pair", lambda p: 0.14 if p == "CNYUSD=X" else None)
    assert abs(fx.get_rate("USD", "CNY") - 1 / 0.14) < 1e-9


def test_yahoo_usd_bridge(monkeypatch):
    no_cfets(monkeypatch)

    def fake(pair):
        return {"HKDUSD=X": 0.128, "USDCNY=X": 7.1}.get(pair)

    monkeypatch.setattr(fx, "_yahoo_pair", fake)
    assert abs(fx.get_rate("HKD", "CNY") - 0.128 * 7.1) < 1e-9


def test_all_fail_returns_none(monkeypatch):
    no_cfets(monkeypatch)
    monkeypatch.setattr(fx, "_yahoo_pair", lambda p: None)
    assert fx.get_rate("HKD", "CNY") is None


def test_cfets_table_parsing(monkeypatch):
    monkeypatch.setattr(fx, "_cfets_cache", (0.0, {}))
    df = pd.DataFrame(
        {
            "货币对": ["USD/CNY", "100JPY/CNY", "EUR/USD"],
            "买报价": [6.70, 4.30, 1.16],
            "卖报价": [6.72, 4.32, 1.17],
        }
    )
    import akshare as ak

    monkeypatch.setattr(ak, "fx_spot_quote", lambda: df)
    table = fx._cfets_table()
    assert table["CNY"] == 1.0
    assert abs(table["USD"] - 6.71) < 1e-9
    assert abs(table["JPY"] - 0.0431) < 1e-9
    assert "EUR" not in table


def test_cfets_cross_rate(monkeypatch):
    monkeypatch.setattr(fx, "_cfets_cache", (0.0, {}))
    monkeypatch.setattr(fx, "_cfets_table", lambda: {"CNY": 1.0, "USD": 6.71, "HKD": 0.855})
    assert abs(fx.get_rate("HKD", "USD") - 0.855 / 6.71) < 1e-9
    assert abs(fx.get_rate("USD", "HKD") - 6.71 / 0.855) < 1e-9


def test_get_fx_rates(monkeypatch):
    no_cfets(monkeypatch)
    monkeypatch.setattr(fx, "_yahoo_pair", lambda p: 7.2 if p == "USDCNY=X" else None)
    rates, missing = fx.get_fx_rates("CNY", ["USD", "CNY", "ZZZ"])
    assert rates == {"USD": 7.2, "CNY": 1.0}
    assert missing == ["ZZZ"]
