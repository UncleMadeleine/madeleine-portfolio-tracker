"""本地磁盘缓存: 行情/基本面持久化到 SQLite, 避免重复网络请求."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock


from .paths import var_dir

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
            pe_dynamic REAL,
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
        con.execute("ALTER TABLE quotes ADD COLUMN pe_dynamic REAL")
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
    # 通用 JSON 快照缓存: 估值快照 / 财报链接等「结构化但非 DataFrame」结果。
    # 与 series_cache 分开是为了按业务前缀独立清理, 且载荷不是 (date, close)。
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS json_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    # 数据源健康事件: 每次真实回源尝试一行 (缓存命中不记). 供 `tracker doctor`
    # 与「数据健康」页聚合成功率/耗时/最近失败; 与行情缓存同库但独立清理,
    # `cache clear` 不删它 —— 排障线索不该被「清缓存」顺手清掉。
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS health_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            ok INTEGER NOT NULL,
            latency_ms REAL,
            symbol TEXT,
            error TEXT
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_health_ts ON health_events(ts)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_health_source "
        "ON health_events(source, kind, ts)"
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


def _with_schema_retry(operation):
    """执行一个 SQLite 操作; 缺表时重建 schema 后重试一次.

    截断/清空后 inode 不变, _db_ready 会误以为表已建好; 所有会直接抛错的
    入口 (info / clear / 单表清理) 都走这里, 避免各处重复同一段重试逻辑。
    """
    for attempt in range(2):
        _ensure_db()
        try:
            return operation()
        except sqlite3.Error as e:
            _forget_ready_on_corruption(e)
            if _is_missing_schema(e) and attempt == 0:
                _forget_ready()
                _ensure_db()
                continue
            raise
    raise RuntimeError("缓存数据库不可用; 稍后重试")


def _read_all(sql: str, params) -> list:
    """只读查询; 锁竞争 (OperationalError, busy timeout 5s 后仍拿不到锁)
    视为「无缓存」返回空列表, 不让裸 traceback 打死 CLI/页面。

    并发场景真实存在: Streamlit 后台刷新线程写库的同时 CLI 或另一页面读库。
    """

    def _fetch():
        with _connection() as con:
            return con.execute(sql, params).fetchall()

    try:
        return _with_schema_retry(_fetch)
    except (sqlite3.Error, OSError):
        return []


def _execute_write(sql: str, params, *, many: bool = False) -> bool:
    """执行写语句; 缓存属可再生的派生数据, 任何 I/O/SQLite 失败都按「未写入」处理.

    缓存目录只读 / 磁盘满 / 库被其它进程锁住时, 不能让一次成功的网络取数
    因为回写缓存失败而整体抛错 (调用方拿到的是行情, 不是缓存状态)。
    """

    def _write():
        with _lock, _connection() as con:
            if many:
                con.executemany(sql, params)
            else:
                con.execute(sql, params)
            con.commit()
        return True

    try:
        return _with_schema_retry(_write)
    except (sqlite3.Error, OSError) as e:
        _forget_ready_on_corruption(e)
        return False


def _read_one(sql: str, params):
    """单行只读查询; 锁竞争/库不可用时返回 None (同 _read_all 语义)。"""
    rows = _read_all(sql, params)
    return rows[0] if rows else None


def _read_payload(
    table: str, where: str, params, ttl: float | None = None
) -> str | None:
    """读取单表 payload; ttl 为 None 时不套 fetched_at 条件.

    table/where 均为模块内常量, 不接收外部输入; 统一走 _read_one 保持
    「锁竞争视为无缓存」与测试 monkeypatch 点不变。
    """
    _ensure_db()
    sql = f"SELECT payload FROM {table} WHERE {where}"
    values = list(params)
    if ttl is not None:
        sql += " AND fetched_at > ?"
        values.append(time.time() - ttl)
    row = _read_one(sql, values)
    return row[0] if row else None


def _write_payload(table: str, key: str, payload: str) -> None:
    """写入单 key payload 表 (series_cache / json_cache / calendar_cache)."""
    _execute_write(
        f"""
        INSERT INTO {table} (key, payload, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """,
        (key, payload, time.time()),
    )


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


# ---------- 数据源健康事件 (取数成功率 / 耗时 / 最近失败) ----------


def add_health_events(rows: list[tuple]) -> bool:
    """批量写入健康事件 (ts, kind, source, ok, latency_ms, symbol, error).

    与行情缓存一样属于「可再生」数据: 写失败不影响取数, 返回 False。
    """
    if not rows:
        return True
    return _execute_write(
        """
        INSERT INTO health_events
            (ts, kind, source, ok, latency_ms, symbol, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
        many=True,
    )


def get_health_events(since: float, limit: int = 20000) -> list[dict]:
    """读取 since 之后**最近**的 limit 条健康事件 (返回按时间升序).

    先按 ts DESC 截断再升序返回: 总行数超过上限时, 丢弃的应是更早的事件;
    直接 `ORDER BY ts ASC LIMIT` 会把最新事件截掉, 健康页/doctor 看起来像
    「最近几小时没有记录」。库忙/损坏时返回空列表, 不让诊断反把 doctor 弄崩。
    """
    rows = _read_all(
        """
        SELECT ts, kind, source, ok, latency_ms, symbol, error
        FROM (
            SELECT ts, kind, source, ok, latency_ms, symbol, error
            FROM health_events WHERE ts >= ?
            ORDER BY ts DESC LIMIT ?
        )
        ORDER BY ts ASC
        """,
        (float(since), int(limit)),
    )
    return [
        {
            "ts": float(ts),
            "kind": str(kind),
            "source": str(source),
            "ok": bool(ok),
            "latency_ms": float(latency_ms) if latency_ms is not None else None,
            "symbol": symbol,
            "error": error,
        }
        for ts, kind, source, ok, latency_ms, symbol, error in rows
    ]


def prune_health_events(before: float, max_rows: int = 20000) -> int:
    """删除 before 之前的事件, 并把总行数压到 max_rows 以内; 返回删除条数."""
    def _prune():
        with _lock, _connection() as con:
            n = con.execute(
                "DELETE FROM health_events WHERE ts < ?", (float(before),)
            ).rowcount
            n += con.execute(
                """
                DELETE FROM health_events WHERE id <= (
                    SELECT MAX(id) - ? FROM health_events
                )
                """,
                (int(max_rows),),
            ).rowcount
            con.commit()
            return n

    try:
        return _with_schema_retry(_prune)
    except (sqlite3.Error, OSError):
        return 0


def clear_health_events() -> int:
    """清空健康事件 (供 doctor --clear 使用), 返回删除条数."""
    def _clear():
        with _lock, _connection() as con:
            n = con.execute("DELETE FROM health_events").rowcount
            con.commit()
            return n

    try:
        return _with_schema_retry(_clear)
    except (sqlite3.Error, OSError):
        return 0


def ohlc_freshness(symbols: list[str], now: float | None = None) -> dict[str, float]:
    """每只代码最近一次 K线缓存年龄 (秒); 无缓存者不在返回值中."""
    if not symbols:
        return {}
    now = time.time() if now is None else now
    out: dict[str, float] = {}
    for chunk in _chunks(list(symbols)):
        rows = _read_all(
            f"SELECT symbol, MAX(fetched_at) FROM ohlc_cache "
            f"WHERE symbol IN ({','.join('?' * len(chunk))}) GROUP BY symbol",
            list(chunk),
        )
        for sym, ts in rows:
            if ts is not None:
                out[str(sym)] = max(0.0, now - float(ts))
    return out


def json_cache_freshness(
    keys: list[str], now: float | None = None
) -> dict[str, float]:
    """给定 json_cache key 的年龄 (秒); 无缓存者不在返回值中.

    与 get_json_cached 不同, 这里不应用 TTL: 供 doctor 区分「从未取过」
    (不在返回值) 与「取过但已过期」(年龄 > TTL)。
    """
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}
    now = time.time() if now is None else now
    out: dict[str, float] = {}
    for chunk in _chunks(unique):
        rows = _read_all(
            f"SELECT key, fetched_at FROM json_cache "
            f"WHERE key IN ({','.join('?' * len(chunk))})",
            list(chunk),
        )
        for key, ts in rows:
            if ts is not None:
                out[str(key)] = max(0.0, now - float(ts))
    return out


def source_distribution() -> dict[str, dict[str, int]]:
    """当前行情缓存按来源统计: {source: {total, fresh}}.

    回答「这次为什么用了 Yahoo 而不是长桥」: 缓存里实际存的是哪一源的结果。
    历史命中记录（health_events）在一次次刷新后会被覆盖, 快照统计补上这一点。
    """
    now = time.time()

    def _query():
        with _connection() as con:
            rows = con.execute(
                """
                SELECT COALESCE(NULLIF(source, ''), 'unknown') AS src,
                       COUNT(*) AS total,
                       SUM(CASE WHEN fetched_at > ? THEN 1 ELSE 0 END) AS fresh
                FROM quotes GROUP BY src ORDER BY total DESC
                """,
                (now - CACHE_TTL,),
            ).fetchall()
        return {str(src): {"total": int(total), "fresh": int(fresh or 0)}
                for src, total, fresh in rows}

    try:
        return _with_schema_retry(_query)
    except (sqlite3.Error, OSError):
        return {}


def quick_check() -> str:
    """SQLite PRAGMA quick_check 结果 ("ok" 表示结构完好)."""
    def _check():
        with _connection() as con:
            row = con.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "unknown"

    try:
        return _with_schema_retry(_check)
    except (sqlite3.Error, OSError) as e:
        return f"不可用: {e}"


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
        ) = _with_schema_retry(_counts)
    except sqlite3.Error as e:
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

    def _delete_all():
        with _lock, _connection() as con:
            n_quotes = con.execute("DELETE FROM quotes").rowcount
            n_ohlc = con.execute("DELETE FROM ohlc_cache").rowcount
            n_fx = con.execute("DELETE FROM fx_rates").rowcount
            n_series = con.execute("DELETE FROM series_cache").rowcount
            n_cal = con.execute("DELETE FROM calendar_cache").rowcount
            n_json = con.execute("DELETE FROM json_cache").rowcount
            con.commit()
            n_fund = con.execute("DELETE FROM fundamentals").rowcount
            con.commit()
            return n_quotes + n_ohlc + n_fund + n_fx + n_series + n_cal + n_json

    try:
        return _with_schema_retry(_delete_all)
    except sqlite3.Error as e:
        raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e

# ---------- 域读写门面 (实现见 cache_domains, 保持既有导入路径) ----------

from .cache_domains import (  # noqa: E402,F401 - 兼容再导出
    EM_VALUE_PREFIX,
    SHARES_FULL_PREFIX,
    _calendar_row_from_payload,
    _clear_table,
    _normalize_quote_currency,
    _valid_fundamental,
    clear_fundamentals,
    clear_fx,
    clear_json_cache,
    clear_ohlc,
    clear_quotes,
    get_cached,
    get_cached_with_age,
    get_calendar_row,
    get_calendar_rows,
    get_fundamental_cached,
    get_fundamentals_cached,
    get_fundamentals_meta,
    get_fx_cached,
    get_json_cached,
    get_ohlc_cached,
    get_series_cached,
    gc_invalid,
    prune_orphans,
    set_cached,
    set_calendar_row,
    set_fundamental_cached,
    set_fx_cached,
    set_json_cached,
    set_ohlc_cached,
    set_series_cached,
)
