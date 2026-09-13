"""A股券商持仓文件导入测试 (纯离线, 不联网)."""
import pytest

from tracker.ashare_sync import code_to_yahoo, parse_positions_file


# ---------- 代码 → Yahoo 后缀映射 ----------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("600519", "600519.SS"),
        ("688981", "688981.SS"),
        ("900902", "900902.SS"),
        ("000001", "000001.SZ"),
        ("002594", "002594.SZ"),
        ("300750", "300750.SZ"),
        ("200012", "200012.SZ"),
        ("830799", "830799.BJ"),
        ("430047", "430047.BJ"),
        ("920100", "920100.BJ"),
        ("SH600519", "600519.SS"),
        ("SZ000001", "000001.SZ"),
    ],
)
def test_code_to_yahoo(code, expected):
    assert code_to_yahoo(code) == expected


def test_code_to_yahoo_rejects_non_a_share():
    assert code_to_yahoo("AAPL") is None
    assert code_to_yahoo("") is None
    assert code_to_yahoo("60051") is None
    # 带 Yahoo 后缀的完整代码不是本函数的输入形态 (券商导出为裸代码)
    assert code_to_yahoo("600519.SS") is None


# ---------- 文件解析 ----------


def _write(tmp_path, text, name="pos.csv", encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


def test_leading_zero_codes_kept(tmp_path):
    """券商导出常省略前导零 (000001); 数值读入会变成 1 而丢失代码."""
    f = _write(
        tmp_path,
        "证券代码,证券名称,持仓数量,成本价\n"
        "000001,平安银行,1000,10.5\n"
        "002594,比亚迪,200,250.3\n"
        "600519,贵州茅台,10,1500\n",
    )
    rows, skipped = parse_positions_file(f)
    assert skipped == []
    assert [r["symbol"] for r in rows] == ["000001.SZ", "002594.SZ", "600519.SS"]
    assert rows[0]["quantity"] == 1000.0
    assert rows[0]["avg_cost"] == 10.5


def test_zero_avg_cost_preserved(tmp_path):
    """成本价为 0 是合法值 (送股/配股后), 不能当成缺失去掉."""
    f = _write(
        tmp_path,
        "证券代码,证券名称,持仓数量,成本价\n600519,贵州茅台,10,0\n",
    )
    rows, _ = parse_positions_file(f)
    assert rows[0]["avg_cost"] == 0.0
    assert rows[0]["type"] == "cn"


def test_missing_cost_column_omits_avg_cost(tmp_path):
    f = _write(
        tmp_path,
        "证券代码,证券名称,持仓数量\n600519,贵州茅台,10\n",
    )
    rows, skipped = parse_positions_file(f)
    assert skipped == []
    assert "avg_cost" not in rows[0]


def test_zero_quantity_rows_skipped(tmp_path):
    f = _write(
        tmp_path,
        "证券代码,证券名称,持仓数量,成本价\n"
        "600519,贵州茅台,0,1500\n"
        "000001,平安银行,100,10\n",
    )
    rows, _ = parse_positions_file(f)
    assert [r["symbol"] for r in rows] == ["000001.SZ"]


def test_gbk_encoded_csv(tmp_path):
    """券商导出多为 GBK/GB2312."""
    f = _write(
        tmp_path,
        "证券代码,证券名称,持仓数量,成本价\n600519,贵州茅台,10,1500\n",
        encoding="gbk",
    )
    rows, skipped = parse_positions_file(f)
    assert skipped == []
    assert rows[0]["symbol"] == "600519.SS"


def test_missing_columns_reports_reason(tmp_path):
    f = _write(tmp_path, "列A,列B\n1,2\n")
    rows, skipped = parse_positions_file(f)
    assert rows == []
    assert any("未找到代码列" in s for s in skipped)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_positions_file(tmp_path / "nope.csv")
