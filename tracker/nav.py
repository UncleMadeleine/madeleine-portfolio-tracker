"""历史净值 (NAV): 每日组合快照的持久化、查询与回填.

存储设计:
- 独立 SQLite (默认 ``<数据目录>/nav_history.db``, 环境变量
  ``TRACKER_NAV_DB`` 可覆盖), 与行情缓存库分离 —— ``cache clear`` 或缓存库
  损坏隔离都不会删掉用户的历史净值。
- 主键 ``(day, base)``: 同一自然日 + 同一基础货币只保留一条, 重复保存覆盖。
- ``source`` 区分两种口径:
    * ``snapshot`` 当日实时快照 (``snapshot --save`` / 组合页自动保存);
    * ``backfill`` 按**当前持仓 × 历史收盘价**估算的回填序列 (不含历史调仓,
      仅用于“如果一直持有现在的仓位, 过去大概是什么走势”)。

净值历史是用户数据, 不是可再生的缓存: 读写失败不能静默当作“没有历史”,
写路径抛出可读错误, 读路径返回空列表以免页面/CLI 因一个坏文件整页崩掉。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from .paths import data_home

_LOCK = threading.Lock()

#: 回填时最多取多少个自然日之前的历史 (防止手误传入超大值把数据源拖死)
MAX_BACKFILL_DAYS = 3650

_COLUMNS = (
    "day",
    "base",
    "total_value",
    "total_cost",
    "total_pnl",
    "total_pnl_pct",
    "today_pnl",
    "cost_coverage",
    "holdings",
    "source",
    "recorded_at",
)


def nav_db_path() -> Path:
    """历史净值库路径 (每次调用解析, 便于测试/容器用 TRACKER_NAV_DB 隔离)."""
    raw = os.environ.get("TRACKER_NAV_DB")
    if raw:
        return Path(raw).expanduser()
    return data_home() / "nav_history.db"


def _create_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS nav_snapshots (
            day TEXT NOT NULL,
            base TEXT NOT NULL,
            total_value REAL NOT NULL,
            total_cost REAL,
            total_pnl REAL,
            total_pnl_pct REAL,
            today_pnl REAL,
            cost_coverage REAL,
            holdings INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'snapshot',
            payload TEXT,
            recorded_at REAL NOT NULL,
            PRIMARY KEY (day, base)
        )
        """
    )
    con.commit()


def _ensure_db() -> None:
    """建目录/建表; 目录不可写时让调用方按空历史处理."""
    path = nav_db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        with sqlite3.connect(path, timeout=5) as con:
            _create_schema(con)
    except sqlite3.Error:
        # 读路径由调用方解释为空; 写路径会再次连接并抛出真实错误
        return


def _num(v) -> float | None:
    """有限浮点归一: None/NaN/Inf/不可解析 → None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _clean_day(v) -> str | None:
    """校验 YYYY-MM-DD; 非法日期返回 None."""
    s = _text(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _payload_from_view(view) -> str | None:
    """把持仓明细压缩成 JSON 存进快照 (便于日后做收益归因)."""
    if view is None or getattr(view, "empty", True):
        return None
    cols = [
        c
        for c in (
            "symbol",
            "name",
            "market",
            "currency",
            "quantity",
            "price",
            "market_value",
            "cost",
            "pnl",
        )
        if c in view.columns
    ]
    records: list[dict] = []
    for raw in view[cols].to_dict(orient="records"):
        rec: dict = {}
        for key, value in raw.items():
            if key in ("symbol", "name", "market", "currency"):
                rec[key] = _text(value)
            else:
                rec[key] = _num(value)
        records.append(rec)
    try:
        return json.dumps(records, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None


def record_from_summary(
    day: str,
    base: str,
    summary: dict,
    view=None,
    *,
    source: str = "snapshot",
) -> dict:
    """把 ``summarize()`` 的结果整理成一条可落库的净值记录.

    ``view`` 可为 None (如回填) —— 此时不写持仓明细 payload。
    """
    total_value = _num(summary.get("total_value"))
    if total_value is None:
        total_value = 0.0
    return {
        "day": _clean_day(day),
        "base": _text(base) or "CNY",
        "total_value": total_value,
        "total_cost": _num(summary.get("total_cost")),
        "total_pnl": _num(summary.get("total_pnl")),
        "total_pnl_pct": _num(summary.get("total_pnl_pct")),
        "today_pnl": _num(summary.get("today_pnl")),
        "cost_coverage": _num(summary.get("cost_coverage")),
        "holdings": int(len(view))
        if view is not None
        else int(summary.get("holdings") or 0),
        "source": source if source in ("snapshot", "backfill") else "snapshot",
        "payload": _payload_from_view(view),
    }


def save_rows(rows, *, overwrite: bool = True) -> int:
    """批量写入净值记录, 返回实际新增/更新的条数.

    ``overwrite=False`` 用于回填: 已有真实快照的日期保持不变 (真实快照优先),
    已有的回填行允许被重新回填更新 (改了持仓后重跑能修正旧估算)。
    写失败抛 RuntimeError (带可读原因), 由 CLI 转成用户可见错误。
    """
    prepared = []
    now = time.time()
    for row in rows or []:
        day = _clean_day(row.get("day"))
        base = _text(row.get("base"))
        value = _num(row.get("total_value"))
        if not day or not base or value is None:
            continue
        payload = row.get("payload")
        if payload is not None and not isinstance(payload, str):
            try:
                payload = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, default=str
                )
            except (TypeError, ValueError):
                payload = None
        prepared.append(
            (
                day,
                base.upper(),
                value,
                _num(row.get("total_cost")),
                _num(row.get("total_pnl")),
                _num(row.get("total_pnl_pct")),
                _num(row.get("today_pnl")),
                _num(row.get("cost_coverage")),
                int(row.get("holdings") or 0),
                row.get("source")
                if row.get("source") in ("snapshot", "backfill")
                else "snapshot",
                payload,
                _num(row.get("recorded_at")) or now,
            )
        )
    if not prepared:
        return 0

    update_set = """
                total_value=excluded.total_value,
                total_cost=excluded.total_cost,
                total_pnl=excluded.total_pnl,
                total_pnl_pct=excluded.total_pnl_pct,
                today_pnl=excluded.today_pnl,
                cost_coverage=excluded.cost_coverage,
                holdings=excluded.holdings,
                source=excluded.source,
                payload=excluded.payload,
                recorded_at=excluded.recorded_at
    """
    if overwrite:
        conflict = " ON CONFLICT(day, base) DO UPDATE SET" + update_set
    else:
        # 回填: 真实快照优先; 已有的回填行允许被重新回填更新 (改持仓后重跑)
        conflict = (
            " ON CONFLICT(day, base) DO UPDATE SET"
            + update_set
            + " WHERE nav_snapshots.source = 'backfill'"
        )

    sql = (
        "INSERT INTO nav_snapshots "
        "(day, base, total_value, total_cost, total_pnl, total_pnl_pct, "
        "today_pnl, cost_coverage, holdings, source, payload, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" + conflict
    )
    _ensure_db()
    try:
        with _LOCK, sqlite3.connect(nav_db_path(), timeout=5) as con:
            _create_schema(con)
            before = con.total_changes
            con.executemany(sql, prepared)
            con.commit()
            return con.total_changes - before
    except sqlite3.Error as e:
        raise RuntimeError(f"历史净值库写入失败 ({e}); 请检查磁盘空间/权限") from e


def _rows_to_dicts(rows) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        rec = dict(zip(_COLUMNS, row))
        rec["total_value"] = _num(rec.get("total_value"))
        rec["total_cost"] = _num(rec.get("total_cost"))
        rec["total_pnl"] = _num(rec.get("total_pnl"))
        rec["total_pnl_pct"] = _num(rec.get("total_pnl_pct"))
        rec["today_pnl"] = _num(rec.get("today_pnl"))
        rec["cost_coverage"] = _num(rec.get("cost_coverage"))
        rec["holdings"] = int(rec.get("holdings") or 0)
        out.append(rec)
    return out


def load_history(base: str, days: int | None = None) -> list[dict]:
    """读取某基础货币的净值历史, 按日期升序.

    ``days`` 为回看自然日数 (含今天): ``days=90`` 只看今天及之前 89 天。
    None / 0 表示全部。库不存在/损坏/被锁时返回空列表。
    """
    base = (_text(base) or "").upper()
    if not base:
        return []
    _ensure_db()
    sql = (
        "SELECT day, base, total_value, total_cost, total_pnl, total_pnl_pct, "
        "today_pnl, cost_coverage, holdings, source, recorded_at "
        "FROM nav_snapshots WHERE base = ?"
    )
    params: list = [base]
    if days:
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            cutoff = (date.today() - timedelta(days=n - 1)).isoformat()
            sql += " AND day >= ?"
            params.append(cutoff)
    sql += " ORDER BY day ASC"
    try:
        with sqlite3.connect(nav_db_path(), timeout=5) as con:
            rows = con.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError):
        return []
    return _rows_to_dicts(rows)


def latest_day(base: str) -> str | None:
    """最近一条净值记录的日期 (无记录/读失败返回 None)."""
    base = (_text(base) or "").upper()
    if not base:
        return None
    _ensure_db()
    try:
        with sqlite3.connect(nav_db_path(), timeout=5) as con:
            row = con.execute(
                "SELECT MAX(day) FROM nav_snapshots WHERE base = ?", (base,)
            ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# 历史回填: 当前持仓 × 历史收盘价
# ---------------------------------------------------------------------------


def _holdings_by_symbol(portfolio: dict) -> tuple[dict[str, dict], list[str]]:
    """按代码聚合持仓: 数量求和, 成本金额 (qty×avg_cost) 求和.

    成本只要有一条腿缺 ``avg_cost``, 该代码的 ``has_cost`` 就置 False ——
    部分成本会被误当成完整成本, 拉低覆盖率并算出虚高盈亏。
    """
    from .symbols import parse

    agg: dict[str, dict] = {}
    issues: list[str] = []
    for h in portfolio.get("holdings", []):
        if not isinstance(h, dict):
            continue
        raw = str(h.get("symbol") or "").strip()
        if not raw:
            continue
        try:
            p = parse(raw)
        except ValueError as e:
            issues.append(f"{raw}: {e}")
            continue
        qty = _num(h.get("quantity"))
        if qty is None:
            issues.append(f"{p.yahoo}: 数量无法解析 ({h.get('quantity')!r}), 回填跳过")
            continue
        if qty == 0:
            continue
        avg = _num(h.get("avg_cost"))
        item = agg.setdefault(
            p.yahoo,
            {"symbol": p.yahoo, "quantity": 0.0, "cost": 0.0, "has_cost": True},
        )
        item["quantity"] += qty
        if avg is None:
            item["has_cost"] = False
        else:
            item["cost"] += qty * avg
    return agg, issues


def _fx_series(currency: str, base: str, start: str, end: str, index):
    """某币种 → 基础货币的历史折算序列 (失败回退当前即期汇率)."""
    import pandas as pd

    from . import fx as fx_mod

    currency = (currency or "").upper()
    base = (base or "").upper()
    if currency == base:
        return pd.Series(1.0, index=index, dtype=float)
    df = fx_mod.get_fx_history(currency, base, start, end)
    if df is not None and not df.empty and "close" in df.columns:
        s = df.copy()
        s["date"] = _normalize_dates(s["date"])
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        s = s.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last")
        if not s.empty:
            series = s.set_index("date")["close"].reindex(index)
            # 历史汇率缺失日 (周末/两地假期) 用最近已知值; 序列起点之前用首个值
            return series.ffill().bfill()
    rate = fx_mod.get_rate(currency, base)
    if rate is None or not math.isfinite(float(rate)) or float(rate) <= 0:
        return None
    return pd.Series(float(rate), index=index, dtype=float)


def _normalize_dates(values):
    """日期列 → 无时区、归一到 00:00 的 datetime (跨数据源对齐用)."""
    import pandas as pd

    dates = pd.to_datetime(values, errors="coerce")
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    return dates.dt.normalize()


def backfill(
    portfolio: dict,
    base: str,
    days: int = 365,
    *,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
    today: date | None = None,
) -> tuple[list[dict], list[str]]:
    """按当前持仓 × 历史收盘价回填净值序列.

    口径提醒: 只使用**今天的持仓数量**, 不会还原历史调仓; 成本按当前
    ``avg_cost`` 计, 因此曲线适合看“长期持有到现在”的近似走势, 不能当作
    真实历史业绩。返回 (rows, issues), rows 可直接交给 ``save_rows``。
    """
    import pandas as pd

    from . import prices
    from .symbols import parse

    base = (_text(base) or "CNY").upper()
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 365
    days = max(1, min(days, MAX_BACKFILL_DAYS))
    end_day = today or date.today()
    start_day = end_day - timedelta(days=days)
    start_s, end_s = start_day.isoformat(), end_day.isoformat()

    agg, issues = _holdings_by_symbol(portfolio)
    if not agg:
        return [], issues or ["无持仓, 无可回填数据"]

    symbols = list(agg)
    try:
        quotes, errors, _notes = prices.get_quotes(
            symbols,
            prefer_akshare=prefer_akshare,
            use_ibkr=use_ibkr,
            use_longport=use_longport,
            crypto_source=crypto_source,
        )
    except Exception as e:  # noqa: BLE001 - 行情失败仍可按代码后缀推断币种
        quotes, errors = {}, {s: str(e) for s in symbols}
    for sym, reason in errors.items():
        issues.append(f"{sym}: 行情缺失 ({reason}), 回填按代码后缀推断币种")
    # 报价币种优先 (港股人民币/美元柜台、B 股等按数据源自报), 否则按后缀推断
    for sym, item in agg.items():
        q = quotes.get(sym)
        item["currency"] = (
            getattr(q, "currency", None) or parse(sym).currency or "USD"
        ).upper()

    months = max(1, math.ceil(days / 30) + 1)
    closes: dict[str, "pd.Series"] = {}
    for sym, item in agg.items():
        try:
            df = prices.get_ohlc(
                sym,
                months=months,
                prefer_akshare=prefer_akshare,
                use_ibkr=use_ibkr,
                use_longport=use_longport,
                crypto_source=crypto_source,
            )
        except Exception as e:  # noqa: BLE001 - 单代码失败不拖垮整轮回填
            issues.append(f"{sym}: 历史行情获取失败 ({e})")
            continue
        if df is None or df.empty or "close" not in df.columns:
            issues.append(f"{sym}: 无历史行情, 回填跳过")
            continue
        s = df[["date", "close"]].copy()
        s["date"] = _normalize_dates(s["date"])
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        s = s.dropna(subset=["date", "close"])
        s = s[s["date"] >= pd.Timestamp(start_day)]
        s = s.drop_duplicates("date", keep="last")
        if s.empty:
            issues.append(f"{sym}: 区间内无历史行情, 回填跳过")
            continue
        closes[sym] = s.set_index("date")["close"].astype(float)

    if not closes:
        return [], issues or ["无可用历史行情, 未生成回填数据"]

    index = pd.DatetimeIndex(
        sorted(set().union(*(set(s.index) for s in closes.values())))
    )
    index = index[(index >= pd.Timestamp(start_day)) & (index <= pd.Timestamp(end_day))]
    if index.empty:
        return [], issues or ["区间内无交易日, 未生成回填数据"]

    # 各币种历史汇率只取一次; 同一币种的多只持仓共用
    fx_cache: dict[str, "pd.Series | None"] = {}
    for item in agg.values():
        ccy = item["currency"]
        if ccy not in fx_cache:
            fx_cache[ccy] = _fx_series(ccy, base, start_s, end_s, index)

    value_cols: dict[str, "pd.Series"] = {}
    cost_cols: dict[str, "pd.Series"] = {}
    for sym, close in closes.items():
        item = agg[sym]
        fx_s = fx_cache.get(item["currency"])
        if fx_s is None:
            issues.append(f"{sym}: 缺少 {item['currency']}→{base} 历史汇率, 回填跳过")
            continue
        # 交易日对齐: 不同市场假期不同, 缺失日沿用最近收盘价
        s = close.reindex(index).ffill()
        value_cols[sym] = s * float(item["quantity"]) * fx_s
        if item["has_cost"]:
            cost_cols[sym] = pd.Series(
                float(item["cost"]) * fx_s, index=index, dtype=float
            )

    if not value_cols:
        return [], issues or ["无可用持仓数据, 未生成回填数据"]
    values = pd.DataFrame(value_cols, index=index)
    total = values.sum(axis=1, min_count=1)
    # 成本金额 = Σ(qty×avg_cost) 按历史汇率折算, 与价格无关;
    # 浮动盈亏只覆盖有成本数据的持仓 (与 summarize 的 cost_mask 同口径)
    if cost_cols:
        costs = pd.DataFrame(cost_cols, index=index).sum(axis=1, min_count=1)
        covered_value = values[[c for c in cost_cols if c in values.columns]].sum(
            axis=1, min_count=1
        )
    else:
        costs = pd.Series(float("nan"), index=index)
        covered_value = pd.Series(float("nan"), index=index)
    pnl = covered_value - costs

    recorded_at = time.time()
    rows: list[dict] = []
    for ts in index:
        value = _num(total.get(ts))
        if value is None:
            continue
        cov_value = _num(covered_value.get(ts))
        cost = _num(costs.get(ts))
        pnl_v = _num(pnl.get(ts))
        coverage = (
            (cov_value / value)
            if cov_value is not None and value not in (None, 0)
            else None
        )
        rows.append(
            {
                "day": ts.date().isoformat(),
                "base": base,
                "total_value": value,
                "total_cost": cost,
                "total_pnl": pnl_v,
                "total_pnl_pct": (pnl_v / abs(cost))
                if pnl_v is not None and cost
                else None,
                "today_pnl": None,
                "cost_coverage": coverage,
                "holdings": int(values.loc[ts].notna().sum()),
                "source": "backfill",
                "recorded_at": recorded_at,
            }
        )
    if not rows:
        issues.append("区间内无有效净值, 未生成回填数据")
    return rows, issues
