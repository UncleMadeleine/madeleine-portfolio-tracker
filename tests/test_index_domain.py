"""指数域 (IX.<KEY>) 测试: 代码解析/路由隔离/provider 源链/K线门面 (不联网)."""

from __future__ import annotations

import pandas as pd
import pytest

from tracker import prices
from tracker.providers import PROVIDERS, provider_for
from tracker.providers.base import resolve
from tracker.providers.index import IndexProvider
from tracker.symbols import INDEX_CATALOG, Market, index_label, parse, type_for_symbol


# ---------- 代码解析 ----------


def test_index_symbols_parse_to_index_domain():
    p = parse("IX.DXY")
    assert p.yahoo == "IX.DXY"
    assert p.market is Market.INDEX
    assert p.type == "index"
    assert p.currency == "USD"


def test_index_parse_case_insensitive():
    assert parse("ix.vix").yahoo == "IX.VIX"
    assert parse(" Ix.Csi300 ").yahoo == "IX.CSI300"


def test_index_cn_entries_expose_akshare_code():
    assert (
        parse("IX.CSI300").ak_code == "000300"
        or INDEX_CATALOG["CSI300"]["ak"] == "sh000300"
    )


def test_unknown_index_key_rejected():
    with pytest.raises(ValueError, match="未收录的指数代码"):
        parse("IX.NOPE")


def test_type_for_symbol_routes_index_exclusively():
    assert type_for_symbol("IX.NDX") == "index"
    # 指数与股票/加密货币互不误判
    assert type_for_symbol("AAPL") == "global"
    assert type_for_symbol("600519.SS") == "cn"
    assert type_for_symbol("BTC-USD") == "crypto"


def test_index_symbols_never_leak_into_stock_domains():
    for key in INDEX_CATALOG:
        assert parse(f"IX.{key}").type == "index"
        assert parse(f"IX.{key}").market is Market.INDEX


# ---------- provider 路由 ----------


def test_resolve_routes_index_domain():
    assert resolve("index").name == "index"
    assert isinstance(resolve("index"), IndexProvider)
    assert isinstance(provider_for(Market.INDEX), IndexProvider)
    assert PROVIDERS["index"] is resolve("index")


def test_index_provider_source_chains():
    provider = IndexProvider()
    # 中国指数: akshare 优先 (与 A 股域数据源惯例一致)
    cn = parse("IX.SSE")
    chains = provider.history_sources(cn, "2025-01-01", None)
    assert chains[0].__name__ == "ak"  # akshare 新浪源优先
    assert chains[1].__name__ == "yf"
    # 目录中无 ak 代码且不在美股新浪表的指数: 仅 yfinance
    vix = parse("IX.VIX")
    assert len(provider.history_sources(vix, "2025-01-01", None)) == 1


def test_ccement_catalog_entries_parse_to_index_domain():
    """水泥网 8 指数: 目录合法 + 统一带 ccement 路由字段 (无 ak/yf 源)."""
    ccement_keys = [k for k, e in INDEX_CATALOG.items() if "ccement" in e]
    assert len(ccement_keys) == 8
    for key in ccement_keys:
        p = parse(f"IX.{key}")
        assert p.type == "index"
        assert p.market is Market.INDEX
        e = INDEX_CATALOG[key]
        assert "ak" not in e and "yf" not in e


def test_ccement_source_chain_is_exclusive():
    """水泥网指数源链只有专属 cc 源, 不落入 yfinance/akshare 误查."""
    provider = IndexProvider()
    chains = provider.history_sources(parse("IX.CEMPI"), "2025-01-01", None)
    assert [f.__name__ for f in chains] == ["cc"]


# ---------- 商品期货源 (碳酸锂主力连续) ----------


def test_futures_catalog_entries_parse_to_index_domain():
    """商品期货指数: 目录合法 + 统一带 futures 路由字段 (无 ak/yf/ccement 源)."""
    futures_keys = [k for k, e in INDEX_CATALOG.items() if "futures" in e]
    assert futures_keys == ["AU", "AG", "JD", "M", "A", "B", "LH", "LC"]
    for key in futures_keys:
        p = parse(f"IX.{key}")
        assert p.type == "index"
        assert p.market is Market.INDEX
        e = INDEX_CATALOG[key]
        assert "ak" not in e and "yf" not in e and "ccement" not in e


def test_futures_entries_use_matching_sina_main_continuous():
    """futures 代码必须是同名品种的新浪主连 (<KEY>0), 代码错位会静默画出别的品种."""
    for key, e in INDEX_CATALOG.items():
        if "futures" not in e:
            continue
        assert e["futures"] == f"{key}0", f"IX.{key} 的主连代码应为 {key}0"
        assert e.get("currency"), f"IX.{key} 缺少报价单位"


def test_futures_source_chain_is_exclusive():
    """商品期货源链只有专属 ft 源 (主力连续无第二数据源)."""
    provider = IndexProvider()
    chains = provider.history_sources(parse("IX.LC"), "2025-01-01", None)
    assert [f.__name__ for f in chains] == ["ft"]


def test_us_index_sina_fallback_uses_matching_index(monkeypatch):
    """美股指数的新浪兜底代码必须与目录同名指数一致.

    NDX = 纳斯达克 100, 新浪代码是 .NDX; .IXIC 是纳斯达克综合指数 (成分与
    点位都不同)。映射错会在 yfinance 失败时静默展示另一条指数。
    """
    from tracker.providers import index as idx_mod

    assert idx_mod._AK_US_SINA == {
        "SPX": ".INX",
        "NDX": ".NDX",
        "DJI": ".DJI",
    }

    calls: list[str] = []

    class FakeAk:
        @staticmethod
        def index_us_stock_sina(symbol):
            calls.append(symbol)
            return pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-10-14", "2026-10-15"]),
                    "open": [1.0, 1.1],
                    "high": [1.2, 1.3],
                    "low": [0.9, 1.0],
                    "close": [1.1, 1.2],
                    "volume": [0.0, 0.0],
                }
            )

    monkeypatch.setattr(idx_mod, "_ak", lambda: FakeAk)
    df = idx_mod._ak_index_history(parse("IX.NDX"), "2026-10-01", None)
    assert calls == [".NDX"]
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2


def test_futures_history_renames_sina_columns(monkeypatch):
    """新浪中文列名 → date/open/high/low/close/volume, 持仓量/动态结算价丢弃."""
    from tracker.providers import index as idx_mod

    class FakeAk:
        @staticmethod
        def futures_main_sina(symbol):
            assert symbol == "LC0"
            return pd.DataFrame(
                {
                    "日期": pd.date_range("2026-10-13", periods=3, freq="D"),
                    "开盘价": [104200.0, 106000.0, 99980.0],
                    "最高价": [108260.0, 106700.0, 102480.0],
                    "最低价": [102000.0, 101000.0, 98000.0],
                    "收盘价": [105480.0, 102800.0, 100540.0],
                    "成交量": [314846.0, 339357.0, 307605.0],
                    "持仓量": [422392.0, 427300.0, 418547.0],
                    "动态结算价": [105480.0, 102800.0, 100540.0],
                }
            )

    monkeypatch.setattr(idx_mod, "_ak", lambda: FakeAk)
    df = idx_mod._ak_futures_history(parse("IX.LC"), "2026-10-14", None)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2  # 本地切片: 只保留 >= start_date
    assert df.iloc[-1]["close"] == pytest.approx(100540.0)
    assert df.iloc[-1]["volume"] == pytest.approx(307605.0)


def test_futures_history_rejects_empty_series(monkeypatch):
    """接口返回空数据必须报错, 不能产出空 DataFrame 静默通过."""
    from tracker.providers import index as idx_mod

    class FakeAk:
        @staticmethod
        def futures_main_sina(symbol):
            return pd.DataFrame()

    monkeypatch.setattr(idx_mod, "_ak", lambda: FakeAk)
    with pytest.raises(RuntimeError, match="空数据"):
        idx_mod._ak_futures_history(parse("IX.LC"), "2025-01-01", None)


def test_ccement_points_parses_dynamic_index_all(monkeypatch):
    """getPriceIndex 聚合载荷用 dynamicIndexAll (独立端点用 dynamicIndex), 都要能解析."""
    from tracker.providers import index as idx_mod

    d = {
        "dynamicIndexDate": ["2026-09-22", "2026-09-23"],
        "dynamicIndexAll": [95.77, 95.95],
    }
    df = idx_mod._ccement_points(d)
    assert list(df.columns) == ["date", "close", "open", "high", "low", "volume"]
    assert df.iloc[-1]["close"] == pytest.approx(95.95)
    assert (df["volume"] == 0.0).all()
    # 独立端点键名: dynamicIndex (可能是 JSON 字符串)
    d2 = {"dynamicIndexDate": ["2026-09-23"], "dynamicIndex": "[291.28]"}
    df2 = idx_mod._ccement_points(d2)
    assert df2.iloc[-1]["close"] == pytest.approx(291.28)


def test_ccement_kline_parses_weekly_rows(monkeypatch):
    """cementkline 行结构 → date/open/high/low/close/volume, 涨跌列丢弃."""
    from tracker.providers import index as idx_mod

    rows = [[1789948800000, 95.77, 95.95, 95.77, 95.95, 95.77, 0.18, 0.19]]
    df = idx_mod._ccement_kline(rows)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df.iloc[0]["close"] == pytest.approx(95.95)
    assert df.iloc[0]["date"] == pd.Timestamp("2026-09-21")


def test_ccement_history_rejects_empty_series(monkeypatch):
    """接口返回空序列必须报错, 不能产出空 DataFrame 静默通过."""
    from tracker.providers import index as idx_mod

    monkeypatch.setattr(
        idx_mod,
        "_ccement_post",
        lambda path, data: {"dynamicIndexDate": [], "dynamicIndex": []},
    )
    with pytest.raises(RuntimeError, match="空序列"):
        idx_mod._ccement_history(parse("IX.CSPI"), "2025-01-01", None)


def test_index_provider_rejects_quote():
    with pytest.raises(NotImplementedError):
        IndexProvider().fetch_quote(parse("IX.DXY"), prefer_first=False)


# ---------- 恒生官方 API 源 (港股组 + AH股溢价指数) ----------


# 恒生官网内部指数代码 → 目录 key (代码错位会静默画出别的指数, 与 futures
# 测试同思路锁死映射; 全部为实测可用代码, 见目录港股组注释)
_HSI_CODE_MAP = {
    "HSI": "00001.00",
    "HSCEI": "00014.00",
    "HSTECH": "02083.00",
    "HSCCI": "00015.00",
    "HSCI": "00011.00",
    "HSHK35": "00020.00",
    "HSHKI": "02019.00",
    "VHSI": "01050.00",
    "HSIII": "02016.00",
    "HSHCI": "02017.00",
    "HSCGSI": "02018.00",
    "HSBIO": "02077.00",
    "HSAHP": "01044.00",
}


def test_hsi_catalog_entries_parse_to_index_domain():
    """恒生官网指数: 目录合法 + 统一带 hsi 路由字段 (无 ak/yf/ccement/futures 源)."""
    hsi_keys = [k for k, e in INDEX_CATALOG.items() if "hsi" in e]
    assert hsi_keys == list(_HSI_CODE_MAP)
    for key in hsi_keys:
        p = parse(f"IX.{key}")
        assert p.type == "index"
        assert p.market is Market.INDEX
        e = INDEX_CATALOG[key]
        assert not ({"ak", "yf", "ccement", "futures"} & set(e))
        assert e.get("currency"), f"IX.{key} 缺少币种展示"


def test_hsi_internal_codes_match_catalog():
    """hsi 内部代码与官网目录一一对应 (官网目录接口 data=allIndexes 实测核对)."""
    for key, code in _HSI_CODE_MAP.items():
        assert INDEX_CATALOG[key]["hsi"] == code


def test_hsi_source_chain_is_exclusive():
    """恒生官网指数源链只有专属 hs 源 (官网零鉴权, 全组无第二数据源)."""
    provider = IndexProvider()
    for key in ("HSAHP", "HSI", "HSTECH"):
        chains = provider.history_sources(parse(f"IX.{key}"), "2025-01-01", None)
        assert [f.__name__ for f in chains] == ["hs"]


class _FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _fake_hsi_requests(payload, calls, status=200):
    class FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "params": params, "headers": headers})
            return _FakeResp(payload, status)

    return FakeRequests


_HSI_PAYLOAD = {
    "code": "0",
    "message": "success",
    "data": [
        {
            "indexCode": "01044.00",
            "indexName": "恒生沪深港通AH股溢价指数",
            "valueHistory": [
                {"date": "2026-10-14", "close": "127.12"},
                {"date": "2026-10-15", "close": "128.12"},
                {"date": "2026-10-16", "close": "128.56"},
            ],
        }
    ],
}


def test_hsi_history_parses_value_history_pub(monkeypatch):
    """valueHistoryPub 日收盘 → 平K线 (open=high=low=close), 本地按窗口切片."""
    from tracker.providers import index as idx_mod

    calls = []
    monkeypatch.setattr(idx_mod, "_requests", _fake_hsi_requests(_HSI_PAYLOAD, calls))
    df = idx_mod._hsi_history(parse("IX.HSAHP"), "2026-10-15", None)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2  # 本地切片: 只保留 >= start_date
    assert df.iloc[-1]["close"] == pytest.approx(128.56)
    # 恒生不发布开高低: 平K线, 不虚构振幅
    row = df.iloc[-1]
    assert row["open"] == row["high"] == row["low"] == row["close"]
    assert (df["volume"] == 0.0).all()
    # 请求参数: 恒生内部指数代码 + 中文语言
    assert calls[0]["params"] == {
        "data": "valueHistoryPub",
        "language": "zh-cn",
        "indexCodes": "01044.00",
    }
    assert calls[0]["url"].endswith("/indexes/metadata/v2")


def test_hsi_history_rejects_empty_series(monkeypatch):
    """接口返回空序列必须报错, 不能产出空 DataFrame 静默通过."""
    from tracker.providers import index as idx_mod

    empty = {"code": "0", "message": "success", "data": [{"valueHistory": []}]}
    monkeypatch.setattr(idx_mod, "_requests", _fake_hsi_requests(empty, []))
    with pytest.raises(RuntimeError, match="空序列"):
        idx_mod._hsi_history(parse("IX.HSAHP"), "2025-01-01", None)


def test_hsi_history_rejects_error_code(monkeypatch):
    """恒生错误响应 (HTTP 400 / code != "0") 必须转成异常, 不静默返回空."""
    from tracker.providers import index as idx_mod

    # HTTP 层错误 (如 400 参数错误) 由 raise_for_status 先行抛出
    bad_status = {"type": "hiip:index360:aim:error:common_0406", "title": "Bad Request"}
    monkeypatch.setattr(
        idx_mod, "_requests", _fake_hsi_requests(bad_status, [], status=400)
    )
    with pytest.raises(RuntimeError, match="HTTP 400"):
        idx_mod._hsi_history(parse("IX.HSAHP"), "2025-01-01", None)

    # HTTP 200 但业务码非 "0"
    bad_code = {"code": "COMMON_0406", "message": "invalid query parameters"}
    monkeypatch.setattr(idx_mod, "_requests", _fake_hsi_requests(bad_code, []))
    with pytest.raises(RuntimeError, match="恒生接口返回异常"):
        idx_mod._hsi_history(parse("IX.HSAHP"), "2025-01-01", None)


def test_hsi_history_slices_end_date_inclusive(monkeypatch):
    """end_date 为闭区间: 结束日当天那根 K 线要保留."""
    from tracker.providers import index as idx_mod

    monkeypatch.setattr(idx_mod, "_requests", _fake_hsi_requests(_HSI_PAYLOAD, []))
    df = idx_mod._hsi_history(parse("IX.HSAHP"), "2026-10-14", "2026-10-15")
    assert [d.strftime("%Y-%m-%d") for d in df["date"]] == [
        "2026-10-14",
        "2026-10-15",
    ]


# ---------- Cameco 官网铀价源 ----------


_CAMECO_HTML = """
<html><body>
<table>
  <thead>
    <tr><th>Date</th><th>Uranium Spot Price</th><th>Long-term Uranium Price</th></tr>
  </thead>
  <tbody>
    <tr><td>1988/01/01</td><td>16.40</td><td></td></tr>
    <tr><td>2026/07/31</td><td>86.38</td><td>95.50</td></tr>
    <tr><td>2026/08/31</td><td>89.68</td><td>96.50</td></tr>
    <tr><td>2026/09/30</td><td>89.63</td><td></td></tr>
  </tbody>
</table>
</body></html>
"""


def _fake_text_requests(text, calls, status=200):
    class FakeRequests:
        @staticmethod
        def get(url, headers=None, timeout=None):
            calls.append({"url": url, "headers": headers, "timeout": timeout})
            return _FakeResp(None, status, text=text)

    return FakeRequests


def test_cameco_catalog_entries_parse_to_index_domain():
    """Cameco 铀价: 目录合法 + 统一带 cameco 路由字段 (无其它数据源)."""
    cameco_keys = [k for k, e in INDEX_CATALOG.items() if "cameco" in e]
    assert cameco_keys == ["URANIUM", "URANIUM_LT"]
    for key in cameco_keys:
        p = parse(f"IX.{key}")
        assert p.type == "index"
        assert p.market is Market.INDEX
        e = INDEX_CATALOG[key]
        assert not ({"ak", "yf", "ccement", "futures", "hsi"} & set(e))
        assert e["cameco"] in {"spot", "lt"}
        assert e.get("currency"), f"IX.{key} 缺少报价单位"


def test_cameco_source_chain_is_exclusive():
    """Cameco 铀价源链只有专属 cm 源 (月频无第二数据源)."""
    provider = IndexProvider()
    for key in ("URANIUM", "URANIUM_LT"):
        chains = provider.history_sources(parse(f"IX.{key}"), "2025-01-01", None)
        assert [f.__name__ for f in chains] == ["cm"]


def test_cameco_history_parses_spot_as_monthly_flat_kline(monkeypatch):
    """现货表 → 平K线; 日期统一到月末, 本地切片, 无成交量."""
    from tracker.providers import index as idx_mod

    calls = []
    monkeypatch.setattr(idx_mod, "_requests", _fake_text_requests(_CAMECO_HTML, calls))
    df = idx_mod._cameco_history(parse("IX.URANIUM"), "1988-01-01", "2026-08-31")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    # 月初/月末混用的源日期统一成月末; end_date 闭区间不多取 9 月
    assert [d.strftime("%Y-%m-%d") for d in df["date"]] == [
        "1988-01-31",
        "2026-07-31",
        "2026-08-31",
    ]
    assert df.iloc[-1]["close"] == pytest.approx(89.68)
    row = df.iloc[-1]
    assert row["open"] == row["high"] == row["low"] == row["close"]
    assert (df["volume"] == 0.0).all()
    assert calls[0]["url"] == idx_mod._CAMECO_URL
    assert calls[0]["headers"] == idx_mod._CAMECO_HEADERS


def test_cameco_history_parses_long_term_series(monkeypatch):
    """长协序列独立选择, 缺失月份丢弃 (不能用现货补齐)."""
    from tracker.providers import index as idx_mod

    monkeypatch.setattr(idx_mod, "_requests", _fake_text_requests(_CAMECO_HTML, []))
    df = idx_mod._cameco_history(parse("IX.URANIUM_LT"), "2026-01-01", None)
    assert [d.strftime("%Y-%m-%d") for d in df["date"]] == [
        "2026-07-31",
        "2026-08-31",
    ]
    assert df.iloc[-1]["close"] == pytest.approx(96.50)


def test_cameco_history_rejects_missing_table(monkeypatch):
    """页面改版/无表格必须显式报错, 不能产出空 DataFrame 静默通过."""
    from tracker.providers import index as idx_mod

    monkeypatch.setattr(
        idx_mod,
        "_requests",
        _fake_text_requests("<html><body>no table</body></html>", []),
    )
    with pytest.raises(RuntimeError, match="未找到铀价表格"):
        idx_mod._cameco_history(parse("IX.URANIUM"), "2025-01-01", None)


def test_cameco_history_dedupes_duplicate_month(monkeypatch):
    """源站同一月份重复行只保留一条 (实测 2019-06 有两条相同记录)."""
    from tracker.providers import index as idx_mod

    html = """
    <html><body><table>
      <tr><th>Date</th><th>Uranium Spot Price</th><th>Long-term Uranium Price</th></tr>
      <tr><td>2025/01/01</td><td>10.00</td><td></td></tr>
      <tr><td>2025/01/31</td><td>11.00</td><td></td></tr>
    </table></body></html>
    """
    monkeypatch.setattr(idx_mod, "_requests", _fake_text_requests(html, []))
    df = idx_mod._cameco_history(parse("IX.URANIUM"), "2025-01-01", None)
    assert [d.strftime("%Y-%m-%d") for d in df["date"]] == ["2025-01-31"]
    assert df.iloc[0]["close"] == pytest.approx(11.00)


def test_get_quotes_routes_index_to_explicit_error(monkeypatch):
    """IX.* 混入实时行情查询: 记入明确 errors, 不落入 global 域误查 yfinance."""
    import tracker.providers.orchestration as orch

    def no_batch(parsed):
        raise AssertionError("指数代码不得进入全球域批量行情")

    monkeypatch.setattr(orch, "_yahoo_batch", no_batch)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda q: None)

    quotes, errors, notes = orch.get_quotes(["IX.DXY", "IX.VIX"])
    assert quotes == {}
    assert set(errors) == {"IX.DXY", "IX.VIX"}
    assert all("无实时行情" in msg for msg in errors.values())


def test_slice_range_end_date_is_inclusive_without_extra_day():
    """end_date 为闭区间: 不许多返回结束日次日那根 K 线."""
    from tracker.providers.index import _slice_range

    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5, freq="D"),
            "open": [1.0] * 5,
            "high": [1.0] * 5,
            "low": [1.0] * 5,
            "close": [1.0] * 5,
        }
    )
    out = _slice_range(df, "2026-01-01", "2026-01-03")
    assert [d.strftime("%Y-%m-%d") for d in out["date"]] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    # 无 end_date: 取到最新
    out2 = _slice_range(df, "2026-01-04", None)
    assert len(out2) == 2


# ---------- K线门面 ----------


def _fake_history(symbol, months=12, start_date=None, end_date=None, **kw):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(30)],
            "volume": 0.0,  # 指数无成交量
        }
    )


def test_kline_payload_keeps_real_volume():
    from tracker.charting import clean_ohlc, kline_payload

    df = _fake_history("AAPL")
    df["volume"] = 100.0
    payload = kline_payload(df, "AAPL", show_volume=True)
    assert len(payload["volume"]) == len(clean_ohlc(df))


def test_get_index_history_rejects_non_index_symbol():
    with pytest.raises(ValueError, match="不是指数代码"):
        prices.get_index_history("AAPL")


def test_get_index_history_routes_through_index_provider(monkeypatch):
    calls = []

    def fake_get_history(symbol, months=12, start_date=None, end_date=None, **kw):
        calls.append(symbol)
        return _fake_history(symbol)

    monkeypatch.setattr("tracker.prices.get_history", fake_get_history)
    df = prices.get_index_history("IX.DXY", months=6, refresh=True)
    assert calls == ["IX.DXY"]
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]


# ---------- 绘图: 零成交量省略 ----------


def test_kline_payload_omits_all_zero_volume():
    from tracker.charting import kline_payload

    payload = kline_payload(_fake_history("IX.DXY"), "IX.DXY", show_volume=True)
    assert payload["volume"] == []


def test_kline_payload_keeps_real_volume_with_positive_volume():
    from tracker.charting import clean_ohlc, kline_payload

    df = _fake_history("AAPL")
    df["volume"] = 100.0
    payload = kline_payload(df, "AAPL", show_volume=True)
    assert len(payload["volume"]) == len(clean_ohlc(df))


# ---------- 目录分组 (CLI --list / UI 下拉的唯一数据源) ----------


def test_index_groups_cover_whole_catalog():
    """分组必须覆盖全部目录条目且无重复: 新增指数只允许经 INDEX_GROUPS 加入."""
    from tracker.symbols import INDEX_GROUPS, index_groups

    grouped = [k for keys in index_groups().values() for k in keys]
    assert sorted(grouped) == sorted(INDEX_CATALOG)
    assert len(grouped) == len(set(grouped))
    assert set(INDEX_GROUPS) == set(index_groups())


def test_index_catalog_flattening_preserves_entries():
    """平铺目录条目与分组定义完全一致 (派生不丢字段)."""
    from tracker.symbols import INDEX_GROUPS

    for group, entries in INDEX_GROUPS.items():
        for key, entry in entries.items():
            assert INDEX_CATALOG[key] == entry
            assert index_label(key) == entry["name"]


def test_index_kline_list_prints_groups(capsys):
    """--list 按分组输出, 每组含组名与全部成员."""
    from tracker.symbols import index_groups
    from tracker.cli.index_kline import print_index_catalog

    print_index_catalog()
    out = capsys.readouterr().out
    for group, keys in index_groups().items():
        assert f"[{group}]" in out
        for key in keys:
            assert f"IX.{key}" in out


def test_index_kline_list_json_machine_readable(capsys):
    """--list --json 输出可解析 JSON: total 与逐条 code/name/group 对得上."""
    import json as _json

    from tracker.cli.index_kline import print_index_catalog

    print_index_catalog(as_json=True)
    data = _json.loads(capsys.readouterr().out)
    assert data["total"] == len(INDEX_CATALOG)
    assert len(data["indices"]) == len(INDEX_CATALOG)
    by_code = {e["code"]: e for e in data["indices"]}
    assert by_code["IX.DXY"]["group"] == "美元/利率"
    assert by_code["IX.CEMPI"]["group"] == "水泥网"
    for e in data["indices"]:
        assert e["code"].startswith("IX.")
        assert e["name"] == index_label(e["key"])


def test_infer_native_period_and_available_periods():
    """数据驱动粒度推断: 日频→三级可选, 周K原生数据→日K不可选 (通用兜底)."""
    from tracker import charting

    daily = _fake_index_history_df(freq="D", n=30)
    weekly = _fake_index_history_df(freq="7D", n=12)
    assert charting.infer_native_period(daily) == "daily"
    assert charting.infer_native_period(weekly) == "weekly"
    # 重采样只能单向变粗: 细于原生粒度的周期不可选
    assert charting.available_periods("daily") == ["daily", "weekly", "monthly"]
    assert charting.available_periods("weekly") == ["weekly", "monthly"]
    assert charting.available_periods("monthly") == ["monthly"]
    # 请求细于原生 → 降级; 匹配或更粗 → 保持请求
    assert charting.resolve_period(weekly, "daily") == "weekly"
    assert charting.resolve_period(weekly, "weekly") == "weekly"
    assert charting.resolve_period(weekly, "monthly") == "monthly"
    assert charting.resolve_period(daily, "daily") == "daily"
    # 样本不足 (无法推断) 按 daily 放行, 不虚构降级
    tiny = _fake_index_history_df(freq="D", n=2)
    assert charting.resolve_period(tiny, "daily") == "daily"


def _fake_index_history_df(freq: str, n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq=freq)
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(n)],
            "volume": 0.0,
        }
    )
