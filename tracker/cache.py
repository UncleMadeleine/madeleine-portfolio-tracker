"""本地磁盘缓存: 行情/基本面持久化到 SQLite, 避免重复网络请求."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import pandas as pd

from .paths import var_dir
from .symbols import is_currency_code, parse

if TYPE_CHECKING:
    from .providers.base import Quote


# 行情缓存落在数据目录 var/ (安装位置无关); TRACKER_CACHE_DB 可显式指定
# (测试隔离 / 只读挂载场景), 缺省 var/quotes_cache.db
def _cache_db_path() -> Path:
    """TRACKER_CACHE_DB 覆盖路径; 相对路径按 paths 模块同口径忽略.

    相对路径会随 CWD 变成不同的库, 用户会看到「缓存/净值历史消失」,
    与 TRACKER_HOME 的绝对路径要求保持一致。
    """
    raw = os.environ.get("TRACKER_CACHE_DB")
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
    return var_dir() / "quotes_cache.db"


CACHE_DB = _cache_db_path()
CACHE_TTL = 300  # 秒, 实时行情缓存有效期 (新鲜期)
# 陈旧期: 超过新鲜期但未离谱的行情仍可先用后刷 (首屏 stale-while-revalidate)。
# 首屏渲染用 STALE_TTL 读盘秒开, 后台线程再按 CACHE_TTL 的新鲜期重取;
# 冷启动 (两档都 miss) 才阻塞等待。非美股行情本身延迟 15-30 分钟, 30 分钟
# 陈旧数据对「先看到数」是可接受的代价, 且刷新完成即换新。
STALE_TTL = 1800  # 秒, 实时行情陈旧可渲染期 (30 分钟)
OHLC_TTL = 1800  # 秒, 日线K线缓存有效期 (30 分钟; 收盘后数据不变, TTL 过期重取也准)
# 财经日历整批缓存 (cn 报表期 / 长桥按月): 与单票正缓存同口径 (events.CAL_TTL)
CALENDAR_TTL = 21600  # 秒 (6 小时)

# 汇率即期缓存 (base→currency): 与行情同样的新鲜/陈旧两档。CFETS 一次拉齐
# CNY 货币对表, 但 akshare 首次 import alone 就要 ~10s, 冷进程每屏都付这笔钱;
# 落盘后首屏秒取, 后台再刷新。
FX_FRESH_TTL = CACHE_TTL  # 300s
FX_STALE_TTL = STALE_TTL  # 1800s

_lock = Lock()
# 建库/建表只做一次: 原先每次 get_*/set_* 都 connect + CREATE TABLE + 4 次
# ALTER 试探, 单次约 0.2ms; 600 只代码的逐条读取会白付 ~0.2s, 并发写时还会
# 反复参与 schema 锁竞争。用 (dev, ino) 标识库文件: 正常写入不改 inode,
# 删除/重建/隔离损坏库会变 → 自动重新建表。
_db_ready_lock = Lock()
_db_ready: dict[str, tuple[int, int]] = {}


def _db_identity() -> tuple[int, int] | None:
    """库文件身份 (dev, ino); 不存在/不可 stat 返回 None."""
    try:
        st = CACHE_DB.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _connect() -> sqlite3.Connection:
    """统一连接参数: 5s 锁等待 (与 sqlite3 默认一致, 显式写清意图)."""
    return sqlite3.connect(CACHE_DB, timeout=5)


@contextmanager
def _connection():
    """连接上下文: 退出时显式 close.

    不能只用 ``with sqlite3.connect(...)`` —— 它只 commit/rollback, 不关闭
    连接。异常回溯仍持有连接时, 后续纠错/重建会在同一文件上撞
    "database is locked" (截断后重建表的场景实测会卡死)。
    """
    con = _connect()
    try:
        yield con
    finally:
        con.close()


def _create_schema(con) -> None:
    # WAL: 后台刷新线程写库时页面/CLI 仍可并发读, 避免「database is locked」
    # 被 _read_all 当成无缓存 → 重复回源。journal_mode 持久化在库文件里,
    # 只需在建库时设置一次; 只读挂载/不支持 WAL 的文件系统忽略即可。
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            prev_close REAL,
            change_pct REAL,
            currency TEXT,
            book_value REAL,
            market_cap REAL,
            source TEXT,
            fetched_at REAL
        )
        """
    )
    # 旧库无 book_value/source 列: 补列 (ALTER 失败说明已存在, 忽略)
    try:
        con.execute("ALTER TABLE quotes ADD COLUMN book_value REAL")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE quotes ADD COLUMN market_cap REAL")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE quotes ADD COLUMN source TEXT")
    except sqlite3.OperationalError:
        pass
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ohlc_cache (
            symbol TEXT NOT NULL,
            months INTEGER NOT NULL,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (symbol, months)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol TEXT PRIMARY KEY,
            book_value REAL,
            market_cap REAL,
            fetched_at REAL,
            book_value_checked INTEGER NOT NULL DEFAULT 0,
            market_cap_checked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_rates (
            base TEXT NOT NULL,
            currency TEXT NOT NULL,
            rate REAL NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (base, currency)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS series_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    # 财经日历 (财报/分红事件): key 前缀区分来源 (g:<symbol> 单票 / cn:<period> /
    # lp:<market>:<category>:<month> 整批), 载荷均为 {"events": [...]}
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    try:
        con.execute("ALTER TABLE fundamentals ADD COLUMN market_cap REAL")
    except sqlite3.OperationalError:
        pass
    # 指标级「已确认」标记: NULL 既可能是「本次没取到」也可能是「取到了但没有」,
    # 只凭值无法区分。没有标记时, 只缓存到 BVPS 的旧行会挡住后续市值取数
    # (反之亦然), 6 小时内一直返回 None。
    try:
        con.execute(
            "ALTER TABLE fundamentals ADD COLUMN book_value_checked INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    try:
        con.execute(
            "ALTER TABLE fundamentals ADD COLUMN market_cap_checked INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    con.commit()


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    """该异常是否真的表示库文件损坏 (而不是锁竞争/磁盘 I/O 等瞬时错误).

    OperationalError("database is locked") 也是 DatabaseError: 早期实现把
    并发窗口里的锁超时当成损坏, 直接把另一个进程正在写的库 os.replace 隔离,
    缓存被劈成两份。只有 CORRUPT/NOTADB 才走隔离重建。
    """
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name in ("SQLITE_CORRUPT", "SQLITE_NOTADB"):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in ("not a database", "malformed", "corrupt"))


#: IN (?,?,...) 占位符分块大小: SQLite 默认变量上限 32766, 自选股上千时
#: 一条 SQL 会直接 OperationalError("too many SQL variables")。
_CHUNK = 400


def _chunks(seq: list, size: int = _CHUNK):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _is_missing_schema(exc: BaseException) -> bool:
    """是否是「表不存在」(文件被就地截断/清空后仍保留 inode).

    _db_ready 只按 (dev, ino) 判断是否需要建表: 截断成 0 字节的有效空库
    inode 不变, 早期 _ensure_db 会直接短路, 之后每次读写都报 no such table
    且永远不会自愈 (cache clear 也失败)。
    """
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "no such table" in str(exc).lower()
    )


def _forget_ready() -> None:
    with _db_ready_lock:
        _db_ready.pop(str(CACHE_DB), None)


def _forget_ready_on_corruption(exc: BaseException) -> None:
    """运行期发现库损坏时清掉「已建表」标记, 让下次 _ensure_db 走隔离重建。

    (dev, ino) 短路只适合「文件被替换」; 同一 inode 被就地写坏时若不摘掉
    标记, 会一直把 DatabaseError 吞成缓存 miss, cache clear 也无从自愈。
    """
    if isinstance(exc, sqlite3.DatabaseError) and _is_corruption(exc):
        _forget_ready()


def _read_all(sql: str, params) -> list:
    """只读查询; 锁竞争 (OperationalError, busy timeout 5s 后仍拿不到锁)
    视为「无缓存」返回空列表, 不让裸 traceback 打死 CLI/页面。

    并发场景真实存在: Streamlit 后台刷新线程写库的同时 CLI 或另一页面读库。
    """
    for attempt in range(2):
        try:
            with _connection() as con:
                return con.execute(sql, params).fetchall()
        except (sqlite3.Error, OSError) as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                # 截断/清空后表没了: 摘掉就绪标记并重建 schema, 再试一次。
                _forget_ready()
                _ensure_db()
                continue
            return []
    return []


def _execute_write(sql: str, params, *, many: bool = False) -> bool:
    """执行写语句; 缓存属可再生的派生数据, 任何 I/O/SQLite 失败都按「未写入」处理.

    缓存目录只读 / 磁盘满 / 库被其它进程锁住时, 不能让一次成功的网络取数
    因为回写缓存失败而整体抛错 (调用方拿到的是行情, 不是缓存状态)。
    """
    for attempt in range(2):
        try:
            _ensure_db()
            with _lock, _connection() as con:
                if many:
                    con.executemany(sql, params)
                else:
                    con.execute(sql, params)
                con.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                _forget_ready()
                _ensure_db()
                continue
            return False
    return False


def _read_one(sql: str, params):
    """单行只读查询; 锁竞争/库不可用时返回 None (同 _read_all 语义)。"""
    rows = _read_all(sql, params)
    return rows[0] if rows else None


def _ensure_db() -> None:
    key = str(CACHE_DB)
    ident = _db_identity()
    if ident is not None:
        with _db_ready_lock:
            if _db_ready.get(key) == ident:
                # 注意不要用文件大小判断是否真空库: WAL 模式下新 schema 可能
                # 还在 -wal 里, 主库文件仍是 0 字节。表缺失由读写路径捕获
                # "no such table" 后摘标记重建 (见 _read_all/_execute_write)。
                return
    try:
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 只读挂载/无权限目录: 后续 connect 会失败, 读路径按无缓存处理,
        # 写路径由 _execute_write 吞掉, 不影响取数主流程。
        return
    con = None
    try:
        con = _connect()
        _create_schema(con)
        ident = _db_identity()
        if ident is not None:
            with _db_ready_lock:
                _db_ready[key] = ident
    except sqlite3.DatabaseError as exc:
        if not _is_corruption(exc):
            # 锁/IO 等瞬时错误: 不隔离正在使用的库, 交给本次读写自行解释
            # (读路径通常仍可继续, 写路径会抛出真实错误)
            if con is not None:
                con.close()
                con = None
            return
        # 缓存库损坏 (写入中途断电/磁盘满/误写文本): 行情/K线/基本面缓存全是可再生
        # 的派生数据。若不处理, snapshot/quote/kline 与 cache info/clear 全链路都会
        # 因一个坏文件裸 traceback —— 连用户想「cache clear 自愈」的那条路也崩。
        # 备份损坏文件并重建空库, 缓存自然重取。WAL 边车文件 (-wal/-shm)
        # 必须一起隔离: 只搬主文件会让 SQLite 在新空库上重放旧 WAL, 可能
        # 再报 malformed 或把旧库页恢复进来; 备份也不完整。
        stamp = int(time.time())
        quarantined = False
        main = Path(f"{CACHE_DB}")
        try:
            os.replace(main, CACHE_DB.with_name(f"{CACHE_DB.name}.corrupt-{stamp}"))
            quarantined = True
        except OSError:
            try:
                main.unlink()
            except OSError:
                pass
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{CACHE_DB}{suffix}")
            if not sidecar.exists():
                continue
            try:
                os.replace(
                    sidecar,
                    CACHE_DB.with_name(f"{CACHE_DB.name}.corrupt-{stamp}{suffix}"),
                )
            except OSError:
                try:
                    sidecar.unlink()
                except OSError:
                    pass
        # 边车已搬走, 现在可以安全关闭仍指向坏库的旧连接; 否则它会锁住
        # 路径, 下面的重建连接会撞 "database is locked"。
        if con is not None:
            con.close()
            con = None
        try:
            with _connect() as con:
                _create_schema(con)
            ident = _db_identity()
            if ident is not None:
                with _db_ready_lock:
                    _db_ready[key] = ident
        except sqlite3.DatabaseError:
            # 重建仍失败 (如目录只读/磁盘满): 放弃建库, 让调用方按「无缓存」继续
            if quarantined:
                try:
                    CACHE_DB.with_name(f"{CACHE_DB.name}.corrupt-{stamp}").replace(
                        CACHE_DB
                    )
                except OSError:
                    pass
    finally:
        if con is not None:
            con.close()


# ---------- K线 (OHLC) 缓存 ----------


def get_ohlc_cached(
    symbol: str, months: int, ttl: int = OHLC_TTL
) -> pd.DataFrame | None:
    """读取缓存的日线 OHLC; 未命中 / 过期 / 载荷损坏返回 None."""
    _ensure_db()
    row = _read_one(
        "SELECT payload FROM ohlc_cache WHERE symbol=? AND months=? AND fetched_at > ?",
        (symbol, int(months), time.time() - ttl),
    )
    if row is None:
        return None
    try:
        df = pd.DataFrame(json.loads(row[0]))
        if df.empty:
            return None
        if not {"date", "open", "high", "low", "close"}.issubset(df.columns):
            # 旧版/损坏但仍是合法 JSON 的载荷 (只剩 date 等) 若当缓存命中,
            # 调用方会跳过网络重取并在图表/回测处 KeyError。
            return None
        df["date"] = pd.to_datetime(df["date"])
        cols = [
            c
            for c in ("date", "open", "high", "low", "close", "volume")
            if c in df.columns
        ]
        return df[cols]
    except Exception:
        return None


def set_ohlc_cached(symbol: str, months: int, df: pd.DataFrame) -> None:
    """写入日线 OHLC 缓存 (整段 JSON 载荷, 按 symbol+months 覆盖)."""
    if df is None or df.empty:
        return
    _ensure_db()
    recs = df.copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    cols = [
        c
        for c in ("date", "open", "high", "low", "close", "volume")
        if c in recs.columns
    ]
    payload = json.dumps(recs[cols].to_dict(orient="records"))
    _execute_write(
        """
        INSERT INTO ohlc_cache (symbol, months, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol, months) DO UPDATE SET
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """,
        (symbol, int(months), payload, time.time()),
    )


# ---------- 基本面 (每股净资产) 缓存 ----------


def _valid_fundamental(v) -> float | None:
    """基本面数值归一: None/非有限/≤0 → None (与写入侧 _valid 同口径)."""
    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
        return float(v)
    return None


def get_fundamentals_meta(
    symbols: list[str], ttl: int = 21600
) -> dict[str, dict]:
    """批量读取基本面缓存及「指标已确认」标记 (内部使用).

    返回 ``{symbol: {book_value, market_cap, book_value_checked,
    market_cap_checked}}``。``*_checked`` 表示该指标已由数据源确认 (包括
    确认缺失), 用于区分「没取过」和「取过但没有」。驱动层据此只补本次真正
    需要的指标, 又不会把已确认的负缓存当成未命中反复重试。
    """
    unique = list(dict.fromkeys(symbols))
    if not unique:
        return {}
    _ensure_db()
    now = time.time()
    out: dict[str, dict] = {}
    for chunk in _chunks(unique):
        rows = _read_all(
            f"SELECT symbol, book_value, market_cap, "
            f"book_value_checked, market_cap_checked FROM fundamentals "
            f"WHERE symbol IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [*chunk, now - ttl],
        )
        for sym, bv, mc, bv_checked, mc_checked in rows:
            out[sym] = {
                "book_value": _valid_fundamental(bv),
                "market_cap": _valid_fundamental(mc),
                "book_value_checked": bool(bv_checked),
                "market_cap_checked": bool(mc_checked),
            }
    return out


def get_fundamentals_cached(
    symbols: list[str], ttl: int = 21600
) -> dict[str, dict]:
    """批量读取基本面缓存; 只返回未过期项 (未命中/过期不在结果里).

    与逐条 get_fundamental_cached 语义一致, 但一次查询覆盖全部代码 ——
    自选 600+ 时逐条调用要付 600 次连接/建表检查 (~0.2s)。
    负缓存行 (两个指标均为 None) 同样返回, 调用方据此避免 TTL 内重试。
    """
    return {
        sym: {
            "book_value": meta["book_value"],
            "market_cap": meta["market_cap"],
        }
        for sym, meta in get_fundamentals_meta(symbols, ttl=ttl).items()
    }


def get_fundamental_cached(symbol: str, ttl: int = 21600) -> dict | None:
    """读取缓存的基本面指标; 未命中/过期返回 None.

    负缓存 (已确认无有效值的标的) 命中返回
    {"book_value": None, "market_cap": None}, 与「未缓存」(None) 区分,
    避免 TTL 内重复网络请求。
    """
    return get_fundamentals_cached([symbol], ttl=ttl).get(symbol)


def set_fundamental_cached(
    symbol: str,
    book_value: float | None,
    market_cap: float | None = None,
    *,
    book_value_checked: bool | None = None,
    market_cap_checked: bool | None = None,
) -> None:
    """写入基本面缓存; 指标无效 (≤0/NaN) 时也落库 (负缓存, 6h 不重试).

    market_cap 为可选增量: 只补 BVPS 的旧调用方写 None, 由后续 mcap 请求覆盖。
    两个指标各自独立 COALESCE: 本次为 None 的字段保留库中旧值, 不被冲成
    NULL —— 否则 yfinance 只回 marketCap 的那次会把已缓存的 BVPS 抹掉,
    之后 6 小时 PB 提醒全部误报「PB 缺失」(且负缓存阻止重试)。

    ``book_value_checked`` / ``market_cap_checked`` 区分「本次没提供该字段」
    (False, 保留旧值) 与「数据源确认没有该字段」 (True, 写入 NULL 并标记
    已确认)。取数管线拿到完整结果时显式传 True, 手动只补一个指标的旧调用
    方保持默认 (值非 None 时视为已确认, None 时保留旧值)。
    fetched_at 刷新即整行 TTL 重新计期 (负缓存语义保留)。
    """
    _ensure_db()
    now = time.time()

    def _valid(v) -> float | None:
        # 与 storage._clean_rows 同口径: ±Infinity 同样不落库 (json 不认, 且
        # 6h 负缓存期内会在视图/snapshot --json 里炸出非法 JSON)
        if v is None or not math.isfinite(v) or v <= 0:
            return None
        return float(v)

    bv = _valid(book_value)
    mc = _valid(market_cap)
    bv_checked = (
        book_value is not None if book_value_checked is None else book_value_checked
    )
    mc_checked = (
        market_cap is not None
        if market_cap_checked is None
        else market_cap_checked
    )
    _execute_write(
        """
        INSERT INTO fundamentals
            (symbol, book_value, market_cap, fetched_at,
             book_value_checked, market_cap_checked)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            book_value=CASE
                WHEN excluded.book_value_checked
                THEN excluded.book_value
                ELSE COALESCE(excluded.book_value, fundamentals.book_value)
            END,
            market_cap=CASE
                WHEN excluded.market_cap_checked
                THEN excluded.market_cap
                ELSE COALESCE(excluded.market_cap, fundamentals.market_cap)
            END,
            book_value_checked=CASE
                WHEN excluded.book_value_checked THEN 1
                ELSE fundamentals.book_value_checked
            END,
            market_cap_checked=CASE
                WHEN excluded.market_cap_checked THEN 1
                ELSE fundamentals.market_cap_checked
            END,
            fetched_at=excluded.fetched_at
        """,
        (symbol, bv, mc, now, int(bool(bv_checked)), int(bool(mc_checked))),
    )


def _normalize_quote_currency(symbol: str, currency) -> str | None:
    """行情币种归一: 非法占位符回退代码推导的权威币种.

    东财 A 股 f172 返回 "-", 旧版本把 "-" 写进了行情缓存。这里读取时兜底,
    既修复历史脏数据, 也避免把 "-" 交给汇率链 (会拼出 -CNY=X 伪货币对)。
    无法从代码推导币种时返回 None, 调用方按脏数据跳过 (下次回源覆盖)。
    """
    ccy = str(currency or "").strip().upper()
    if is_currency_code(ccy):
        return ccy
    try:
        return parse(symbol).currency
    except ValueError:
        return None


def _finite_opt(value) -> float | None:
    """可选数值归一: None/不可解析/±Inf/NaN → None, 其余 float.

    行情缓存的 price 一直有校验, 但 prev_close/change_pct 曾直接 float():
    历史脏行里的 ``inf`` 会让 analytics 的今日盈亏变成「全部市值」,
    并在 ``--json`` 输出非标准 JSON。
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def get_cached_with_age(
    symbols: list[str], ttl: int = STALE_TTL
) -> tuple[dict[str, "Quote"], dict[str, float]]:
    """返回 (Symbol→Quote, Symbol→age秒) 两个字典; 未命中/过期/脏数据不在返回值中.

    age = now - fetched_at (秒). 首屏 stale-while-revalidate 按 age 分档:
    age ≤ CACHE_TTL 视为新鲜 (渲染即最终结果), 超过则视为陈旧 (先渲染,
    同时后台重取)。ttl 控制最长可接受年龄 (默认陈旧期)。
    """
    from .providers.base import Quote

    if not symbols:
        return {}, {}

    _ensure_db()
    now = time.time()
    hits: dict[str, Quote] = {}
    ages: dict[str, float] = {}
    for chunk in _chunks(list(symbols)):
        rows = _read_all(
            f"SELECT symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, source, fetched_at "
            f"FROM quotes "
            f"WHERE symbol IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [*chunk, now - ttl],
        )
        for sym, name, price, prev, chg, ccy, bv, mc, src, fetched_at in rows:
            if price is not None and math.isfinite(price) and price > 0:
                quote_ccy = _normalize_quote_currency(sym, ccy)
                if quote_ccy is None:
                    continue
                hits[sym] = Quote(
                    symbol=sym,
                    name=name,
                    price=price,
                    prev_close=_finite_opt(prev),
                    change_pct=_finite_opt(chg),
                    currency=quote_ccy,
                    book_value=float(bv) if bv is not None else None,
                    market_cap=float(mc) if mc is not None else None,
                    source=str(src) if src else None,
                )
                ages[sym] = max(0.0, now - float(fetched_at or now))
    return hits, ages


def get_cached(symbols: list[str], ttl: int = CACHE_TTL) -> dict[str, "Quote"]:
    """返回缓存命中的 Symbol→Quote 字典, 未命中者不在返回值中."""
    hits, _ages = get_cached_with_age(symbols, ttl=ttl)
    return hits


def set_cached(quotes: dict[str, "Quote"]) -> None:
    """将 Quote 写入缓存, 更新 fetched_at. 过滤掉 price<=0 的脏数据.

    book_value/market_cap 与 set_fundamental_cached 同口径用 COALESCE 保留
    库中旧值: yfinance/OpenBB 路径不填这两个指标 (恒 None), 无条件覆盖会把
    上一轮 akshare/长桥写进去的 BVPS 与总市值抹成 NULL, 首屏 PB/市值提醒
    随之拿不到值。
    """
    if not quotes:
        return
    _ensure_db()
    now = time.time()
    rows: list[tuple] = []
    for q in quotes.values():
        if q.price is None or not math.isfinite(q.price) or q.price <= 0:
            continue
        quote_ccy = _normalize_quote_currency(q.symbol, q.currency)
        if quote_ccy is None:
            # 币种无法识别/推导: 不落库, 避免污染下一轮缓存读取与汇率链
            continue
        rows.append(
            (
                q.symbol,
                q.name,
                q.price,
                _finite_opt(q.prev_close),
                _finite_opt(q.change_pct),
                quote_ccy,
                q.book_value,
                getattr(q, "market_cap", None),
                getattr(q, "source", None),
                now,
            )
        )
    _execute_write(
        """
        INSERT INTO quotes (symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            -- 名称与基本面字段同样 COALESCE: yfinance/OpenBB 路径常回 name=None,
            -- 无条件覆盖会把上一轮 akshare/长桥/条目写入的中文名抹成 NULL
            -- (自选表/组合明细随后只剩代码)。空串也视为缺失。
            name=COALESCE(NULLIF(excluded.name, ''), quotes.name),
            price=excluded.price,
            prev_close=excluded.prev_close, change_pct=excluded.change_pct,
            currency=excluded.currency, book_value=COALESCE(excluded.book_value, quotes.book_value),
            market_cap=COALESCE(excluded.market_cap, quotes.market_cap),
            source=excluded.source, fetched_at=excluded.fetched_at
        """,
        rows,
        many=True,
    )


# ---------- 汇率 (即期) 缓存 ----------


def get_fx_cached(
    base: str, currencies, ttl: int = FX_STALE_TTL
) -> tuple[dict[str, float], dict[str, float]]:
    """返回 (currency→rate, currency→age秒); base 自身/未命中/过期不在返回值中.

    与 get_cached_with_age 同构: 首屏按 age 分新鲜/陈旧两档处理。
    """
    base = str(base).upper()
    curs = [
        c
        for c in dict.fromkeys(str(x).upper() for x in currencies)
        if c != base and is_currency_code(c)
    ]
    if not curs:
        return {}, {}

    _ensure_db()
    now = time.time()
    rates: dict[str, float] = {}
    ages: dict[str, float] = {}
    for chunk in _chunks(curs):
        rows = _read_all(
            f"SELECT currency, rate, fetched_at FROM fx_rates "
            f"WHERE base=? AND currency IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [base, *chunk, now - ttl],
        )
        for ccy, rate, fetched_at in rows:
            if rate is None or not math.isfinite(rate) or rate <= 0:
                continue
            rates[ccy] = float(rate)
            ages[ccy] = max(0.0, now - float(fetched_at or now))
    return rates, ages


def set_fx_cached(base: str, rates: dict[str, float]) -> None:
    """写入汇率缓存 (base→currency), 刷新 fetched_at; 无效值 (<=0) 与 base 跳过."""
    base = str(base).upper()
    rows = []
    for c, r in rates.items():
        ccy = str(c).upper()
        # 非法币种占位符 ("-" / "NONE" / 空串) 不是货币代码, 落库后会被
        # get_fx_cached 当成真实汇率复用 (旧版本曾把 "-" 写成 CNY 汇率)。
        if not is_currency_code(ccy) or ccy == base:
            continue
        try:
            rate = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0:
            rows.append((base, ccy, rate, time.time()))
    if not rows:
        return
    _execute_write(
        """
        INSERT INTO fx_rates (base, currency, rate, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(base, currency) DO UPDATE SET
            rate=excluded.rate, fetched_at=excluded.fetched_at
        """,
        rows,
        many=True,
    )


def _clear_table(table: str) -> int:
    """清空单张缓存表; 失败统一转成可读 RuntimeError (设置页据此提示).

    运行时损坏 (DatabaseError: file is not a database) 与截断后缺表
    (no such table) 都必须摘掉 _db_ready 并尝试一次重建, 否则本次失败后
    进程内会一直认为库已就绪, 自愈路径永远不触发。
    """
    for attempt in range(2):
        _ensure_db()
        try:
            with _lock, _connection() as con:
                n = con.execute(f"DELETE FROM {table}").rowcount
                con.commit()
                return n
        except sqlite3.Error as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                _forget_ready()
                _ensure_db()
                continue
            raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e
    raise RuntimeError("缓存数据库不可用; 稍后重试")


def clear_fx() -> int:
    """清汇率磁盘缓存: 数据源偏好 (IBKR 开关) 变化后强制按新链路重取."""
    return _clear_table("fx_rates")


def clear_quotes() -> int:
    """只清实时行情缓存 (保留 K线/基本面): 数据源偏好切换后强制按新源链重取."""
    return _clear_table("quotes")


def clear_ohlc() -> int:
    """只清 K线(OHLC) 磁盘缓存: 数据源偏好切换后强制按新源链重取K线.

    缓存键含 symbol + 窗口 + 源偏好/EODHD/source_order 签名, 但设置页切换
    开关或拖动顺序后仍主动全清: 既要清跨版本/旧键的历史数据, 也要让页面的
    数据来源标记随新链路刷新。与 clear_quotes 配套, 由设置页在源变化时调用
    (clear 全清亦可, 但会连基本面一起清)。
    """
    return _clear_table("ohlc_cache")


def clear_fundamentals() -> int:
    """只清基本面缓存 (PB/市值): EODHD fundamentals 角色切换后重取."""
    return _clear_table("fundamentals")


# ---------- 财经日历 (财报/分红事件) 缓存 ----------


def _calendar_row_from_payload(payload: str, fetched_at: float):
    try:
        data = json.loads(payload)
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            return None
    except Exception:
        return None
    return {"events": events, "fetched_at": fetched_at}


def get_calendar_rows(keys: list[str]) -> dict[str, dict]:
    """批量读取日历缓存行; 未命中/载荷损坏的 key 不在结果里.

    与 get_calendar_row 语义一致 (不套 WHERE fetched_at, TTL 交给调用方),
    但一次查询覆盖整批 key —— 日历页逐票循环时避免 N 次连接/建表检查。
    """
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}
    _ensure_db()
    out: dict[str, dict] = {}
    for chunk in _chunks(unique):
        rows = _read_all(
            f"SELECT key, payload, fetched_at FROM calendar_cache "
            f"WHERE key IN ({','.join('?' * len(chunk))})",
            list(chunk),
        )
        for key, payload, fetched_at in rows:
            row = _calendar_row_from_payload(payload, fetched_at)
            if row is not None:
                out[key] = row
    return out


def get_calendar_row(key: str) -> dict | None:
    """读取日历缓存行; 未命中/载荷损坏返回 None.

    返回 {"events": [...], "fetched_at": float}。TTL 判定留给调用方:
    单票缓存按「载荷是否为空」区分正/负缓存两个 TTL (events.py), 整批缓
    存统一 6h —— 这里不套 WHERE fetched_at, 只给原始时间戳。
    """
    return get_calendar_rows([key]).get(key)


def set_calendar_row(key: str, events: list) -> None:
    """写入日历缓存行; 空事件列表同样落库 (单票负缓存依赖空载荷)."""
    _ensure_db()
    payload = json.dumps({"events": events}, ensure_ascii=False, default=str)
    _execute_write(
        """
        INSERT INTO calendar_cache (key, payload, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """,
        (key, payload, time.time()),
    )


# ---------- 通用序列缓存 (历史市值 em / 历史股本 shares 用) ----------


def get_series_cached(key: str, ttl: int) -> pd.DataFrame | None:
    """读取缓存的 (date, close) 序列; 未命中/过期/载荷损坏返回 None.

    与 OHLC 缓存同构: 整段 JSON 载荷按 key 覆盖, TTL 过期重取。
    """
    _ensure_db()
    row = _read_one(
        "SELECT payload FROM series_cache WHERE key=? AND fetched_at > ?",
        (key, time.time() - ttl),
    )
    if row is None:
        return None
    try:
        df = pd.DataFrame(json.loads(row[0]))
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def set_series_cached(key: str, df: pd.DataFrame) -> None:
    """写入 (date, close) 序列缓存; 空表跳过 (不落库空载荷)."""
    if df is None or df.empty:
        return
    _ensure_db()
    recs = df[["date", "close"]].copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    payload = json.dumps(recs.to_dict(orient="records"))
    _execute_write(
        """
        INSERT INTO series_cache (key, payload, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """,
        (key, payload, time.time()),
    )


def info() -> dict:
    """缓存统计: 记录总数 / 有效条数 / 数据库大小.

    库被其它进程独占 (OperationalError) 时抛 RuntimeError 可读错误, 而不是
    裸 traceback —— 否则用户连 `cache info` 这条自检路径都用不了。
    截断/清空后缺表 (no such table) 与 _clear_table 同口径自动重建一次:
    这是用户自查/修复缓存的入口, 不能永久卡在「no such table」。
    """
    now = time.time()

    def _counts():
        with _connection() as con:
            total = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
            fresh = con.execute(
                "SELECT COUNT(*) FROM quotes WHERE fetched_at > ?", (now - CACHE_TTL,)
            ).fetchone()[0]
            # 陈旧期内的条数: 首屏可先渲染后刷新 (stale-while-revalidate)
            usable = con.execute(
                "SELECT COUNT(*) FROM quotes WHERE fetched_at > ?", (now - STALE_TTL,)
            ).fetchone()[0]
            ohlc_total = con.execute("SELECT COUNT(*) FROM ohlc_cache").fetchone()[0]
            ohlc_fresh = con.execute(
                "SELECT COUNT(*) FROM ohlc_cache WHERE fetched_at > ?",
                (now - OHLC_TTL,),
            ).fetchone()[0]
            fx_total = con.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0]
            fx_fresh = con.execute(
                "SELECT COUNT(*) FROM fx_rates WHERE fetched_at > ?",
                (now - FX_FRESH_TTL,),
            ).fetchone()[0]
            oldest, newest = con.execute(
                "SELECT MIN(fetched_at), MAX(fetched_at) FROM quotes"
            ).fetchone()
            series_total = con.execute("SELECT COUNT(*) FROM series_cache").fetchone()[
                0
            ]
            series_fresh = con.execute(
                "SELECT COUNT(*) FROM series_cache WHERE fetched_at > ?",
                (now - OHLC_TTL,),
            ).fetchone()[0]
            cal_total = con.execute(
                "SELECT COUNT(*) FROM calendar_cache"
            ).fetchone()[0]
        return (
            total,
            fresh,
            usable,
            ohlc_total,
            ohlc_fresh,
            fx_total,
            fx_fresh,
            oldest,
            newest,
            series_total,
            series_fresh,
            cal_total,
        )

    for attempt in range(2):
        _ensure_db()
        try:
            (
                total,
                fresh,
                usable,
                ohlc_total,
                ohlc_fresh,
                fx_total,
                fx_fresh,
                oldest,
                newest,
                series_total,
                series_fresh,
                cal_total,
            ) = _counts()
            break
        except sqlite3.Error as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                _forget_ready()
                _ensure_db()
                continue
            raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e
    size = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0
    return {
        "db": str(CACHE_DB),
        "ttl_seconds": CACHE_TTL,
        "stale_ttl_seconds": STALE_TTL,
        "total": total,
        "fresh": fresh,
        "stale": total - fresh,
        "usable": usable,
        "fx_ttl_seconds": FX_FRESH_TTL,
        "fx_stale_ttl_seconds": FX_STALE_TTL,
        "fx_total": fx_total,
        "fx_fresh": fx_fresh,
        "ohlc_ttl_seconds": OHLC_TTL,
        "ohlc_total": ohlc_total,
        "ohlc_fresh": ohlc_fresh,
        "oldest_at": oldest or 0.0,
        "newest_at": newest or 0.0,
        "series_total": series_total,
        "series_fresh": series_fresh,
        "calendar_total": cal_total,
        "calendar_ttl_seconds": CALENDAR_TTL,
        "size_bytes": size,
    }


def clear() -> int:
    """清空全部缓存 (行情 + K线 + 基本面 + 汇率), 返回删除条数."""
    for attempt in range(2):
        _ensure_db()
        try:
            with _lock, _connection() as con:
                n_quotes = con.execute("DELETE FROM quotes").rowcount
                n_ohlc = con.execute("DELETE FROM ohlc_cache").rowcount
                n_fx = con.execute("DELETE FROM fx_rates").rowcount
                n_series = con.execute("DELETE FROM series_cache").rowcount
                n_cal = con.execute("DELETE FROM calendar_cache").rowcount
                con.commit()
                n_fund = con.execute("DELETE FROM fundamentals").rowcount
                con.commit()
                return n_quotes + n_ohlc + n_fund + n_fx + n_series + n_cal
        except sqlite3.Error as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                _forget_ready()
                _ensure_db()
                continue
            raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e
    raise RuntimeError("缓存数据库不可用; 稍后重试")
