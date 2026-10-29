"""本地 JSON 存储层: portfolio.json / watchlist.json / settings.json 唯一读写口.

CLI 与 Streamlit 页面共用同一组函数, 保证 schema 归一与权威 type 打标语义单点化:
任何入口写出的文件, 另一入口读到的结构一致。

- type 字段为系统维护的内部域标记 (global/cn/crypto), 读/写两侧均按
  symbols.type_for_symbol() 推导并覆写, 用户手改 JSON 无效。
- portfolio.json 保存时合并磁盘上已有键 (如 _说明 等文档/自定义字段), 不丢用户数据。
- 写盘统一 json.dumps(..., allow_nan=False): NaN/None 单元格在清洗阶段剔除。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from .paths import data_home
from .symbols import parse, type_for_symbol
from .services.rules import metric_for_entry as rules_metric_for_entry

# 数据文件锚定数据目录 (TRACKER_HOME > 源码树仓库根 > 平台用户目录, 见 paths.py:
# pip 安装后不再写进 site-packages; 源码树跑法与旧版完全一致)
_DATA_HOME = data_home()
PORTFOLIO_PATH = _DATA_HOME / "portfolio.json"
WATCHLIST_PATH = _DATA_HOME / "watchlist.json"
SETTINGS_PATH = _DATA_HOME / "settings.json"

DEFAULT_BASE_CURRENCY = "CNY"


class CorruptDataError(ValueError):
    """数据文件损坏且无法从 .bak 恢复 (CLI 据此给出可读错误而非 traceback)."""


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def backup_file(path: str | Path) -> str | None:
    """备份 <path> 为 <path>.bak; 文件不存在返回 None。导入写盘前统一调用。"""
    p = Path(path)
    if not p.exists():
        return None
    backup = str(p) + ".bak"
    shutil.copy(p, backup)
    return backup


def _read_json(path: str | Path) -> dict:
    # utf-8-sig: 容忍带 BOM 的文件 (部分编辑器保存 JSON 时加 BOM, utf-8 直接解码报错)。
    # 中文 Windows 编辑器 (记事本 ANSI / Excel 另存) 常写成 GBK, 直接按 UTF-8
    # 读会抛 UnicodeDecodeError (非 JSONDecodeError, 原先会绕过 .bak 恢复路径
    # 变成裸 traceback), 故先 UTF-8 再 GBK 兜底。
    p = Path(path)
    text = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise CorruptDataError(f"{path} 编码无法识别 (非 UTF-8/GBK 文本)")
    data = json.loads(
        text,
        # 手改/旧版/第三方工具可能写入非标准 JSON 字面量 NaN/Infinity/-Infinity
        # (json.loads 默认接受)。读侧归一成 None, 与写侧 allow_nan=False 口径
        # 一致: 否则下一次保存同一个文件会抛
        # "Out of range float values are not JSON compliant" 而失败。
        parse_constant=lambda _c: None,
    )
    # 合法 JSON 但顶层不是对象 (如手滑把数组/字符串写进 portfolio.json):
    # 不能静默当 {} —— 调用方 (_read_json_or_recover) 会据此想恢复/报错,
    # 回 {} 会让下一次保存直接把可恢复的内容覆盖成空组合
    if not isinstance(data, dict):
        raise CorruptDataError(
            f"{path} 顶层不是 JSON 对象 (实际类型: {type(data).__name__})"
        )
    return data


def _read_json_or_recover(path: str | Path) -> dict:
    """读取 JSON; 文件损坏时尝试同名 .bak 恢复, 仍失败则抛出可读错误。

    不静默返回空数据 —— 否则下一次保存会把损坏文件直接覆盖成空组合, 造成数据丢失。
    """

    def _recover(e: Exception, reason: str) -> dict:
        bak = Path(f"{path}.bak")
        if bak.exists():
            try:
                return _read_json(bak)
            except (json.JSONDecodeError, CorruptDataError, OSError):
                pass
        raise CorruptDataError(
            f"{path} {reason}; "
            f"已尝试从 {bak.name} 恢复但失败, 请修复或删除该文件后重试。"
        ) from e

    try:
        return _read_json(path)
    except json.JSONDecodeError as e:
        return _recover(e, f"不是合法 JSON ({e.msg}, 第 {e.lineno} 行)")
    except CorruptDataError as e:
        # _read_json 对「顶层非对象」直接抛 CorruptDataError: 这里走同一条
        # .bak 恢复路径, 不能静默当空数据 (原因信息含具体文件与类型)
        return _recover(e, str(e))
    except OSError as e:
        # 权限/IO 错误: .bak 通常不可读同一路径, 直接给可读错误而不是裸 traceback
        raise CorruptDataError(f"{path} 读取失败 ({e.strerror or e})") from e


def _write_json(data: dict, path: str | Path) -> None:
    """原子写: 先写同目录临时文件再 os.replace, 中断不会把原文件截断成半截 JSON。"""
    p = Path(path)
    # 父目录不存在 (全新安装未跑过 tracker init / TRACKER_HOME 指向未建目录)
    # 时 mkstemp 直接 FileNotFoundError 裸 traceback; 与 cli/_common.
    # atomic_write_text 同口径先建目录。
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    # 保存前把当前内容刷新成 .bak: _read_json_or_recover 在主文件损坏时直接
    # 读 .bak 当恢复结果, 若 .bak 还是数月前导入时的快照, 恢复会把期间的
    # 全部编辑静默丢掉。备份失败 (只读目录等) 不阻断保存本身。
    try:
        backup_file(p)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 建出的临时文件是 0600, os.replace 会原样带给正式文件 ——
        # 容器内跨 UID 读 (CLI 与 Streamlit 不同用户) 会失败。按普通 open()
        # 的语义落权限: 已存在则保留原 mode, 新建则 0666 & ~umask。
        try:
            os.chmod(tmp, p.stat().st_mode & 0o7777 if p.exists() else 0o666 & ~_umask())
        except OSError:
            pass
        os.replace(tmp, p)
        # fsync 父目录: 只 fsync 文件不保证 rename 的目录项落盘, 掉电后可能
        # 表现为"最后一次保存丢了" (旧内容还在, 不会损坏)。
        try:
            dfd = os.open(str(p.parent), getattr(os, "O_DIRECTORY", os.O_RDONLY))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _umask() -> int:
    """读取当前 umask (os.umask 是读-写原子对, 取完立刻还原)。"""
    cur = os.umask(0o022)
    os.umask(cur)
    return cur


def _require_entry_list(data: dict, key: str, path: str | Path) -> list:
    """校验 data[key] 是「对象数组」; 结构畸形抛 CorruptDataError。

    合法 JSON 但结构畸形 (如 {"holdings": ["AAPL"]} / {"watchlist": [null]})
    时, 下游 dict(r) 会抛裸 ValueError/TypeError —— 既不可读, 也绕过了
    _read_json_or_recover 的 .bak 恢复路径。这里统一转成 CorruptDataError,
    自动获得「可读错误 + .bak 恢复」。
    """
    raw = data.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or any(not isinstance(x, dict) for x in raw):
        raise CorruptDataError(
            f"{path} 的 {key} 结构畸形 (应为对象数组, "
            f"实际类型: {type(raw).__name__})"
        )
    return raw


def _stamp_type(row: dict) -> dict:
    """条目补写权威 type 字段 (系统维护, 用户不可见不可改)。

    symbol 先经 parse().yahoo 归一 (别名后缀 .SG→.SI, .SH→.SS, 港股补零等):
    手写/导入的别名与规范形态不会在文件里裂成两条; parse 失败保留原串。
    """
    sym = str(row.get("symbol", "")).strip()
    if sym:
        try:
            row["symbol"] = parse(sym).yahoo
        except Exception:
            pass
        try:
            row["type"] = type_for_symbol(sym)
        except Exception:
            pass
    return row


# ---------------------------------------------------------------------------
# portfolio.json
# ---------------------------------------------------------------------------


def load_portfolio(path: str | Path = PORTFOLIO_PATH) -> dict:
    """读取持仓文件; 文件缺失时返回空组合。

    存在文件时也补齐 base_currency/holdings 两个必需键: 手写/旧版/第三方
    导入的文件可能缺键, 直接 `data["base_currency"]` 的调用方 (如 compare
    --metric mcap) 会抛 KeyError。
    """
    if not Path(path).exists():
        return {"base_currency": DEFAULT_BASE_CURRENCY, "holdings": []}
    data = _read_json_or_recover(path)
    data.setdefault("base_currency", DEFAULT_BASE_CURRENCY)
    data["holdings"] = _require_entry_list(data, "holdings", path)
    return data


def _clean_rows(rows: list) -> list[dict]:
    """剔除 NaN/None 单元格, 保证 allow_nan=False 可序列化 (data_editor 空单元格等).

    ±Infinity 同样过不了 json.dumps(allow_nan=False); 嵌套 dict/list 里的
    非有限值也会让整行炸掉 (如 source_quantities 台账), 故递归清洗:
    顶层非有限值整键剔除 (原语义), 嵌套非有限值置 null。

    非映射行 (手写 JSON 里混进字符串/数字) 直接跳过: dict(r) 会抛裸
    ValueError ("dictionary update sequence element #0 has length 1"),
    写盘整条失败。
    """

    def _clean(v):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clean(x) for x in v]
        return v

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        clean = {}
        for k, v in dict(r).items():
            if v is None:
                continue
            v = _clean(v)
            if v is None:
                continue
            clean[k] = v
        out.append(clean)
    return out


def save_portfolio(data: dict, path: str | Path = PORTFOLIO_PATH) -> None:
    """保存持仓/基础货币 (写盘唯一入口)。

    保留文件中其它键 (_说明 等文档/自定义字段), holdings 逐行清洗并覆写权威 type。
    """
    p = Path(path)
    if p.exists():
        # 损坏/不可读的旧文件不能静默当 {} —— 同 _read_json_or_recover 注释:
        # 直接覆盖会把 _说明 等自定义字段连同原持仓一起清掉 (数据丢失),
        # 走 .bak 恢复, 仍失败则抛可读错误。
        merged = _read_json_or_recover(p)
    else:
        merged = {}
    merged.update(data)
    merged.setdefault("base_currency", DEFAULT_BASE_CURRENCY)
    merged["holdings"] = [
        _stamp_type(r) for r in _clean_rows(merged.get("holdings", []))
    ]
    _write_json(merged, p)


# ---------------------------------------------------------------------------
# watchlist.json
# ---------------------------------------------------------------------------


def parse_lists(v) -> list[str]:
    """把逗号分隔字符串/列表解析为归属列表; 空则回退默认。"""
    if v is None or (isinstance(v, float) and v != v):  # NaN
        return ["默认"]
    if isinstance(v, list):
        # 元素里的 None / NaN / ±Inf 经 str() 会变成 "None"/"nan"/"inf" 成为
        # 一个列表名 (自选股悄悄归到叫 "nan" 的列表, --list 默认 永远匹配不到),
        # 与 rules._num 同口径剔除。
        items = []
        for x in v:
            if x is None or (isinstance(x, float) and not math.isfinite(x)):
                continue
            s = str(x).strip()
            if s.lower() in ("nan", "inf", "-inf", "infinity", "-infinity", "none"):
                continue
            items.append(s)
    else:
        items = [x.strip() for x in str(v).replace("，", ",").split(",")]
    return [x for x in items if x] or ["默认"]


def normalize_watch_entry(e: dict) -> dict:
    """自选条目 schema 归一: 旧阈值键名迁移 + metric 归一 + symbol 归一 + 权威 type 覆写。

    symbol 经 parse().yahoo 归一 (别名后缀 .SG→.SI, 港股补零 700→0700,
    .SH→.SS 等): 手写/导入的别名与规范形态不会在文件里裂成两条。
    metric 归一为 price/pb/mcap; price 为缺省基准不落盘, 非法值剔除。
    手写市值别名 market_cap 统一收敛为 mcap 键 (与 pb 口径一致), 覆盖值保留。
    parse 失败 (无法识别的代码) 时保留原串, 由取数/视图层报错。
    """
    out = dict(e)
    # 旧阈值键名迁移: 两个键并存时也删除旧键 (否则旧键永久留在文件里成为
    # 所有消费者都忽略的死数据), 新键已存在则保留新键。
    for old, new in (("upper", "upper_1"), ("lower", "lower_1")):
        if old in out:
            out.setdefault(new, out.pop(old))
    # lists 可能是手写/第三方写入的逗号分隔字符串: 统一解析为列表,
    # 否则 dedupe/save 路径按字符迭代 ("科技" → ["科", "技"]) 或 append 崩溃。
    if "lists" in out:
        out["lists"] = parse_lists(out.get("lists"))
    # 手写市值别名收敛: 保留数值, 去掉别名键 (mcap 为唯一形态)
    if "market_cap" in out and out.get("market_cap") not in (None, ""):
        out.setdefault("mcap", out.pop("market_cap"))
    else:
        out.pop("market_cap", None)
    metric = rules_metric_for_entry(out)
    raw_metric = out.get("metric")
    if raw_metric not in (None, "") or metric != "price":
        # 显式指定过基准 (含 price) 或推断出非 price: 落盘钉死。遗留的
        # mcap/pb 覆盖键会在下次加载时被 metric_for_entry 按键推断, 不钉死
        # 的话显式选的 price 基准会被推断回 mcap/pb (往返不稳定)
        out["metric"] = metric
    else:
        out.pop("metric", None)  # 缺省基准不落盘, 文件保持干净
    sym = str(out.get("symbol", "")).strip()
    if sym:
        try:
            out["symbol"] = parse(sym).yahoo
        except Exception:
            pass
    return _stamp_type(out)


def dedupe_watch_entries(entries: list[dict]) -> list[dict]:
    """按归一 symbol 去重并合并 (lists 并集; 其余字段非空者补齐).

    save/load 共用: 写侧去重保证文件不裂条, 读侧去重保证外部编辑/旧文件里
    的同代码多条不会在 report/snapshot/UI 被重复计数 (同一提醒触发两次)。
    条目内容保持原样 (只归一去重键), 不改变读路径既有语义。
    """
    by_symbol: dict[str, dict] = {}
    for raw in entries:
        e = dict(raw)
        sym = str(e.get("symbol", "")).strip()
        if not sym:
            continue
        try:
            key = parse(sym).yahoo
        except Exception:
            key = sym.upper()
        found = by_symbol.get(key)
        if found is None:
            by_symbol[key] = e
            continue
        names = e.get("lists")
        if isinstance(names, str):  # 防御: 直接调用本函数时 lists 可能未归一
            names = parse_lists(names)
        found_lists = found.get("lists")
        if isinstance(found_lists, str):
            found_lists = parse_lists(found_lists)
            found["lists"] = found_lists
        elif found_lists is None:
            found_lists = []
            found["lists"] = found_lists
        for name in names or []:
            if name not in found_lists:
                found_lists.append(name)
        for k, v in e.items():
            if k in ("symbol", "lists", "type"):
                continue
            # 仅当尚无该键或原值为空 (None/"") 时才覆盖; 显式 0 (如 upper_1=0)
            # 是有效值, 不能被后一条的 truthy 判断当空值抹掉
            if v is not None and v != "" and (
                k not in found or found[k] in (None, "")
            ):
                found[k] = v
    return list(by_symbol.values())


def load_watchlist(path: str | Path = WATCHLIST_PATH) -> dict:
    """读取自选; 兼容旧扁平/分组格式并迁移为扁平条目, 读取侧补写权威 type.

    读出去重 (合并语义同 save_watchlist): 外部编辑/旧文件可能在同代码上
    留多条, 读路径若不去重会把同一提醒重复计数。
    """
    p = Path(path)
    if not p.exists():
        return {"watchlist": []}
    raw = _read_json_or_recover(p)
    if "watchlist" in raw:
        entries = []
        for e in _require_entry_list(raw, "watchlist", path):
            e = dict(e)
            e["lists"] = parse_lists(e.get("lists"))
            entries.append(normalize_watch_entry(e))
        return {"watchlist": dedupe_watch_entries(entries)}
    if "watchlists" in raw:
        groups = raw["watchlists"]
        if not isinstance(groups, dict) or any(
            not isinstance(v, list) or any(not isinstance(e, dict) for e in v)
            for v in groups.values()
        ):
            raise CorruptDataError(
                f"{path} 的 watchlists 结构畸形 (应为 对象→对象数组)"
            )
        merged: dict[str, dict] = {}
        for name, lst in raw["watchlists"].items():
            for e in lst:
                sym = str(e.get("symbol", "")).strip()
                if not sym:
                    continue
                # 去重键用归一后的规范代码: 别名 (D05.SG/D05.SI) 与规范形态
                # (D05.SI) 跨组出现时并 lists 而不是裂成两条
                try:
                    key = parse(sym).yahoo
                except Exception:
                    key = sym
                if key not in merged:
                    merged[key] = dict(e)
                    merged[key].setdefault("lists", [])
                else:
                    # 同名代码在多个列表中各有一份阈值/备注: 保留先出现的,
                    # 缺失字段 (如只在旧列表设过 upper) 从后出现的补齐
                    for k, v in e.items():
                        if k == "symbol" or k == "lists":
                            continue
                        merged[key].setdefault(k, v)
                if name not in merged[key]["lists"]:
                    merged[key]["lists"].append(name)
        entries = []
        for e in merged.values():
            if not e["lists"]:
                e["lists"] = ["默认"]
            # normalize_watch_entry 返回副本 (不改原对象), 必须接收返回值
            entries.append(normalize_watch_entry(e))
        return {"watchlist": entries}
    return {"watchlist": []}


def save_watchlist(data: dict, path: str | Path = WATCHLIST_PATH) -> None:
    """保存自选 (写盘唯一入口): 逐条 schema 归一 + 权威 type 覆写 + 同代码去重。

    与 save_portfolio 一致: 保留文件中已有其它键 (_说明 等文档/自定义字段)。
    symbol 经 normalize_watch_entry 归一后按代码去重 (lists 并集, 其余字段
    非空者补齐): 别名 (如 D05.SG/D05.SI) 与规范形态不会在文件里裂成两条。
    """
    p = Path(path)
    if p.exists():
        # 与 save_portfolio 一致: 损坏文件不静默当 {} 覆盖掉已有数据。
        merged = _read_json_or_recover(p)
    else:
        merged = {}
    merged["watchlist"] = dedupe_watch_entries(
        # _clean_rows: 与 save_portfolio 同一道清洗 —— data_editor 空单元格 /
        # CLI 传入 nan/inf 会在 json.dumps(allow_nan=False) 处直接抛
        # ValueError ("Out of range float values are not JSON compliant"),
        # 让保存整条失败。非有限阈值/覆盖值应直接剔除, 而不是炸写盘。
        _clean_rows(
            [
                normalize_watch_entry(dict(raw))
                for raw in data.get("watchlist", [])
                if isinstance(raw, dict)
            ]
        )
    )
    _write_json(merged, p)


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------

# 涨跌配色: cn = 红涨绿跌 (A股软件习惯, 默认) / intl = 绿涨红跌 (国际配色)
SCHEME_CN = "cn"
SCHEME_INTL = "intl"
# 加密货币域数据源偏好: auto = 默认链 (Binance→Gate.io→Hyperliquid→yfinance),
# 其余值把对应源前置 (链内其余源按默认相对顺序兜底)
CRYPTO_SOURCES = ("auto", "binance", "gate", "hyperliquid", "yfinance")
DEFAULT_SETTINGS = {
    "color_scheme": SCHEME_CN,
    "prefer_akshare": False,
    "use_ibkr": False,
    "use_longport": False,
    "crypto_source": "auto",
}


def load_settings(path: str | Path = SETTINGS_PATH) -> dict:
    """读取设置; 文件缺失/损坏时回退默认值, 未知键与非法值忽略。"""
    data = dict(DEFAULT_SETTINGS)
    p = Path(path)
    if p.exists():
        try:
            # utf-8-sig: 兼容编辑器存成的带 BOM settings.json, 否则 json.loads
            # 报错被吞 → 全部设置静默回退默认 (与 portfolio/watchlist 读取一致)
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            raw = None
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    if data["color_scheme"] not in (SCHEME_CN, SCHEME_INTL):
        data["color_scheme"] = SCHEME_CN
    data["prefer_akshare"] = bool(data["prefer_akshare"])
    data["use_ibkr"] = bool(data["use_ibkr"])
    data["use_longport"] = bool(data.get("use_longport"))
    crypto_source = str(data.get("crypto_source") or "auto").strip().lower()
    if crypto_source not in CRYPTO_SOURCES:
        crypto_source = "auto"
    data["crypto_source"] = crypto_source
    return data


def save_settings(data: dict, path: str | Path = SETTINGS_PATH) -> None:
    """保存设置 (写盘唯一入口): 与磁盘已有键/默认键合并, 不丢未知键。"""
    p = Path(path)
    # 与 save_portfolio/save_watchlist 一致: 保留文件中已有自定义键 (_说明 等),
    # 只覆盖本次写入的已知键。settings 属可重建的轻量配置, 损坏且无 .bak 时
    # 不阻断保存 (load_settings 同样回退默认值), 避免设置页彻底不可写。
    try:
        merged = _read_json_or_recover(p) if p.exists() else {}
    except CorruptDataError:
        merged = {}
    merged.update({**DEFAULT_SETTINGS, **data})
    _write_json(merged, p)
