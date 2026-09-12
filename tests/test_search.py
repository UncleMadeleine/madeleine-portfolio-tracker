"""股票搜索模块测试 (纯函数, 不依赖网络)."""
import pytest

from tracker.search import SymbolEntry, _cn_suffix, _match, _score, search_symbols


# ---------- A股代码 → 交易所后缀 ----------


@pytest.mark.parametrize(
    "code, suffix",
    [
        ("600519", "SS"),  # 沪市主板
        ("688981", "SS"),  # 科创板
        ("000001", "SZ"),  # 深市主板
        ("300750", "SZ"),  # 创业板
        ("002594", "SZ"),  # 中小板
        ("900902", "SS"),  # 沪市B股
        ("200012", "SZ"),  # 深市B股
        ("830799", "BJ"),  # 北交所
        ("430047", "BJ"),  # 北交所
    ],
)
def test_cn_suffix(code, suffix):
    assert _cn_suffix(code) == suffix


# ---------- 评分函数 ----------


def test_score_exact_code():
    assert _score("AAPL", "AAPL", "APPLE INC") == 1000


def test_score_prefix_code():
    assert _score("AA", "AAPL", "APPLE INC") == 500


def test_score_substring_code():
    # 代码子串 (200) + 名称子串 (150): "pl" 同时在 "aapl" 和 "apple inc" 中
    assert _score("PL", "AAPL", "APPLE INC") == 200 + 150


def test_score_exact_name():
    # 名称精确匹配 (800) 高于代码子串 (200+150)
    assert _score("apple inc", "AAPL", "APPLE INC") == 800


def test_score_combined_code_and_name():
    # 代码精确匹配 (1000); "aapl" 不在 "apple inc" 中, 名称不加分
    assert _score("aapl", "AAPL", "APPLE INC") == 1000


def test_score_no_match():
    assert _score("XYZ", "AAPL", "APPLE INC") == 0


# ---------- 模糊匹配 ----------


@pytest.fixture
def sample_entries():
    return [
        SymbolEntry("AAPL", "苹果", "美股"),
        SymbolEntry("600519.SS", "贵州茅台", "A股"),
        SymbolEntry("000001.SZ", "平安银行", "A股"),
        SymbolEntry("0700.HK", "腾讯控股", "港股"),
        SymbolEntry("AAP", "苹果公司", "美股"),
        SymbolEntry("601318.SS", "中国平安", "A股"),
    ]


def test_match_by_name_chinese(sample_entries):
    res = _match(sample_entries, "茅台", limit=5)
    assert len(res) == 1
    assert res[0].code == "600519.SS"
    assert res[0].name == "贵州茅台"


def test_match_by_name_ambiguous(sample_entries):
    res = _match(sample_entries, "苹果", limit=5)
    # 精确名称匹配 (AAPL·苹果, 800) 排在包含匹配 (AAP·苹果公司, 150) 之前
    assert res[0].code == "AAPL"
    assert any(e.code == "AAP" for e in res)


def test_match_by_code_prefix(sample_entries):
    res = _match(sample_entries, "AAPL", limit=5)
    assert res[0].code == "AAPL"  # 精确代码 (1000)


def test_match_by_code_partial(sample_entries):
    res = _match(sample_entries, "600", limit=5)
    codes = [e.code for e in res]
    assert "600519.SS" in codes  # "600" 是 "600519.SS" 的前缀


def test_match_limit(sample_entries):
    res = _match(sample_entries, "A", limit=2)
    assert len(res) <= 2


def test_match_empty_query(sample_entries):
    assert _match(sample_entries, "", limit=5) == []


def test_match_no_results(sample_entries):
    assert _match(sample_entries, "不存在的股票XYZ", limit=5) == []


def test_match_case_insensitive(sample_entries):
    res = _match(sample_entries, "aapl", limit=5)
    assert res[0].code == "AAPL"


def test_match_ranking_exact_before_prefix(sample_entries):
    # 精确代码 (1000) 应排在代码前缀 (500) 之前
    res = _match(sample_entries, "AAPL", limit=5)
    assert res[0].code == "AAPL"
    # AAP 是前缀匹配, 应在 AAPL 之后
    if len(res) > 1:
        assert res[1].code == "AAP"


# ---------- search_symbols (含降级) ----------


def test_search_symbols_with_entries(sample_entries):
    res = search_symbols("茅台", entries=sample_entries)
    assert len(res) == 1
    assert res[0]["code"] == "600519.SS"
    assert res[0]["name"] == "贵州茅台"
    assert res[0]["market"] == "A股"


def test_search_symbols_empty_entries_fallback_valid_code():
    # 无缓存时降级: 合法代码经 parse 校验后返回 (含市场标签)
    res = search_symbols("AAPL", entries=[])
    assert len(res) == 1
    assert res[0]["code"] == "AAPL"
    assert res[0]["market"] == "美股"


def test_search_symbols_empty_entries_fallback_cn_code():
    res = search_symbols("600519.SS", entries=[])
    assert res[0]["code"] == "600519.SS"
    assert res[0]["market"] == "A股"


def test_search_symbols_empty_entries_fallback_sh_alias():
    # SH 别名应被规范化为 SS
    res = search_symbols("600519.SH", entries=[])
    assert res[0]["code"] == "600519.SS"


def test_search_symbols_empty_entries_invalid():
    # 无缓存 + 未知后缀 → 空结果 (parse 校验失败)
    assert search_symbols("FOO.ZZ", entries=[]) == []


def test_search_symbols_empty_entries_us_no_suffix():
    # 无缓存 + 无后缀代码 → 视为美股返回 (parse 对无后缀代码默认美股)
    res = search_symbols("AAPL", entries=[])
    assert res[0]["code"] == "AAPL"
    assert res[0]["market"] == "美股"


def test_search_symbols_empty_query():
    assert search_symbols("", entries=[]) == []


def test_search_symbols_returns_dicts(sample_entries):
    res = search_symbols("平安", entries=sample_entries)
    assert isinstance(res, list)
    for item in res:
        assert set(item.keys()) == {"code", "name", "market"}
