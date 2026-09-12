# OpenBB Portfolio Tracker

> 本地多市场投资组合追踪，零 API Key，完全免费

[中文](#openbb-portfolio-tracker) · [English](#openbb-portfolio-tracker-en)

---

## 简介

OpenBB Portfolio Tracker 是一款**本地优先**的投资组合追踪工具，支持 **A股 / B股 / 港股 / 美股 / 德股 / 英股 / 加股 / 澳股** 等多市场，提供：

- **多币种持仓追踪** — 自动汇率换算，统一基础货币展示
- **自选股价格阈值提醒** — 两级上限/下限，触发分级，距离预测
- **IBKR 行情接入** — TWS / IB Gateway 实时快照，自动回退
- **K 线蜡烛图** — 日K / 周K / 月K，MA 均线，成交量，交互式 HTML
- **Streamlit 可视化页面** — 持仓明细、资产配置饼图、走势对比、自选提醒
- **统一 CLI** — 快照、报价、汇率、历史、K线、同步、缓存，全部 `--json` 输出

> **核心设计**：数据源多级降级（IBKR → OpenBB/yfinance → akshare），离线可用的 SQLite 磁盘缓存，所有配置为可编辑 JSON 文件，不依赖任何云服务。

---

## 快速开始

### 环境要求

- Python ≥ 3.10
- pip / venv

### 安装

```bash
cd ~/Project/openbb-portfolio-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 启动 Streamlit 页面

```bash
streamlit run app.py
```

页面默认运行在 `http://localhost:8501`，侧边栏含「投资组合」和「🕯 K线」两个标签页。

### CLI 快速上手

```bash
# 查看持仓 + 自选 + 阈值提醒快照
python -m tracker.cli snapshot

# 切换基础货币（如 USD）
python -m tracker.cli snapshot --base USD

# 只看某个自选列表
python -m tracker.cli snapshot --watchlist 科技

# JSON 输出（机器可读，适合脚本/AI）
python -m tracker.cli snapshot --json

# 查询单个/多个代码实时行情
python -m tracker.cli quote AAPL 600519.SS

# 汇率查询
python -m tracker.cli fx USD CNY EUR

# 近 12 个月历史价格
python -m tracker.cli history AAPL --months 12

# 生成 K 线蜡烛图（HTML 文件）
python -m tracker.cli kline AAPL --months 12
python -m tracker.cli kline 600519.SS --ma 5,10,20,60
python -m tracker.cli kline 0700.HK --period weekly --open

# 自选股管理
python -m tracker.cli watchlist list
python -m tracker.cli watchlist add NVDA --list 科技,美股 --upper1 260
python -m tracker.cli watchlist remove NVDA

# 导出自选监控报告
python -m tracker.cli report
python -m tracker.cli report -f json -o report.json
python -m tracker.cli report -f csv -w 科技

# 行情磁盘缓存管理
python -m tracker.cli cache info
python -m tracker.cli cache clear

# 从 IBKR 同步真实持仓（需 Gateway 已登录，API 已启用）
python -m tracker.cli sync --dry-run          # 仅预览
python -m tracker.cli sync --mode live        # 实盘模式（默认 paper 模拟 4002）

# 运行单元测试（不联网，纯逻辑 mock）
python -m pytest tests/ -q
```

> 完整 CLI 帮助：`python -m tracker.cli --help`（或每个子命令 `--help`）。

---

## 代码规范（Yahoo Finance 后缀）

| 市场 | 后缀 | 示例 | 币种 | 说明 |
|------|------|------|------|------|
| 美股 | 无 | `AAPL` | USD | |
| A股 | `.SS` / `.SZ` | `600519.SS` `000001.SZ` | CNY | 也接受 `.SH` |
| B股 | `.SS` / `.SZ` | `900902.SS` `200012.SZ` | CNY | 上海 B 股 `9` 开头，深圳 B 股 `2` 开头 |
| 港股 | `.HK` | `0700.HK` `0941.HK` | HKD | **4 位补零**；`00700.HK` 自动归一 |
| 德股 | `.DE` 等 | `SAP.DE` | EUR | `.F` `.BE` `.DU` `.HM` `.SG` `.MU` 均可 |
| 英股 | `.L` | `BP.L` | GBP | Yahoo 报价单位为便士（GBp），系统自动 ÷100 换算为英镑 |
| 加股 | `.TO` 等 | `RY.TO` | CAD | `.V`（TSXV）`.CN` `.NE` 均可 |
| 澳股 | `.AX` | `BHP.AX` | AUD | |

`avg_cost`（成本）按**当地货币**填写；英股填**英镑**（如 `4.30 = £4.30`，不是便士）。

---

## 自选股与价格阈值提醒

`watchlist.json` 为扁平条目列表，**一个代码可属于多个列表**（`lists` 数组，逗号分隔多归属），支持**两级阈值**（`upper_1`/`upper_2`、`lower_1`/`lower_2`）与备注。旧格式（扁平 `upper`/`lower`、嵌套 `{"watchlists": {...}}`）自动迁移：

```json
{
  "watchlist": [
    { "symbol": "TSLA", "lists": ["科技", "美股"], "upper_1": 420, "upper_2": 450, "lower_1": 280, "lower_2": 250, "note": "两级提醒" },
    { "symbol": "600036.SS", "lists": ["银行", "A股"], "upper_1": 55, "lower_1": 38, "note": "招商银行" },
    { "symbol": "BRK-B", "lists": ["默认"], "note": "无阈值纯观察" }
  ]
}
```

- **同一代码属于多个列表**：如 TSLA 同时在「科技」和「美股」，配置集中在一处，改一次全生效
- **阈值按当地货币**（与显示的现价同币种），到达或越过（含等于）即触发
- 触发等级（从高到低）：`🔴 突破上限 II` / `🟠 突破上限 I` / `🟡 跌破下限 I` / `🟢 跌破下限 II`；触发项排在最前
- 距离列：`距上限 I%` / `距上限 II%` / `距下限 I%` / `距下限 II%` = 还需变动百分之几才触发（负值 = 已越过）
- 页面侧栏「所属列表」列用逗号分隔编辑多归属；「自选观察」tab 顶部「查看范围」选 `全部` 或单个列表
- CLI：`--watchlist <列表名>` 只看某个列表（`全部` 合并去重）
- 持仓与自选**共用一次批量行情请求**，多加自选不增加请求次数

---

## 架构

```
tracker/
├── __main__.py         python -m tracker 入口（转发到 cli）
├── cli.py              CLI 统一入口（snapshot / quote / watchlist / fx / history / kline / sync / cache）
├── symbols.py          代码解析、市场识别、GBp/港股补零归一
├── prices.py           行情路由：IBKR 优先（批量快照）→ yfinance 批量 → akshare 降级 → 逐个重试
├── fx.py               汇率：CFETS（akshare）优先，yfinance 货币对兜底（直对/逆对/USD 桥）
├── cache.py            行情 SQLite 磁盘缓存（实时 5 分钟 / K线 30 分钟）
├── charting.py         K线蜡烛图（plotly 开源渲染：清洗/均线/周月K/成交量/断轴）
├── analytics.py        组合视图与指标（纯函数）
├── watchlist.py        自选股视图与价格阈值状态（纯函数 + 配置读写）
├── ibkr.py             IBKR 行情接入 + 持仓同步（可选依赖 ib_async，失败静默回退）
├── ibkr_sync.py        CLI：从 IBKR 账户持仓生成 portfolio.json
└── snapshot.py         CLI 快照（持仓 + 自选，支持 --ibkr / --json）

app.py                  Streamlit 页面（持仓编辑/指标/配置/走势/自选提醒/K线查询）
kline_page.py           K线页面逻辑（输入驱动取数）

tests/                  纯逻辑单元测试（mock 数据源，不联网）
portfolio.json          持仓配置（页面可直接编辑保存）
watchlist.json          自选股配置（页面可直接编辑保存）
ibkr.example.json       IBKR 配置模板（随仓库提交）
ibkr.json               IBKR 真实配置（已 gitignore，不随仓库提交）
```

---

## K 线图

页面端基于 TradingView 开源的 **lightweight-charts** 渲染（已 vendored，无新增依赖），券商 App 通用交互；CLI 导出的独立 HTML 仍用 **plotly**：

- **蜡烛图 + 成交量副图 + MA 均线**（默认 MA5/20/60，可自定义），默认红涨绿跌（可切国际配色）
- **拖动 = 平移**（带惯性滚动），**滚轮/捏合 = 缩放时间轴**，触控板双指横滑平移、双指纵向/捏合缩放
- 十字光标 + 顶部 OHLC/涨跌/量/均线信息栏；价格轴随可见区间自动缩放；副图分隔线可拖拽；双击轴复位
- **非交易日断轴**：周末/节假日不出空隙，图形连续
- **日K / 周K / 月K** 一键切换（周K 按 W-FRI 对齐）
- 数据经过**清洗校验**（去重/排序，丢弃 high<low 等自相矛盾的脏行），英股便士自动换算
- **双层缓存**：SQLite 磁盘缓存 30 分钟（`cache info` 可查看），页面另有内存缓存；`--refresh` 强制刷新

```bash
python -m tracker.cli kline AAPL --months 12            # 生成 data/kline_AAPL.html
python -m tracker.cli kline 600519.SS --ma 5,10,20,60   # 自定义均线
python -m tracker.cli kline 0700.HK --period weekly --open  # 周K + 自动打开浏览器
```

页面启动后侧边栏「🕯 K线」标签页可查询**任意代码**（不限于持仓/自选）。数据**不预加载**——输入代码点「查询 K线」才实时拉取（30 分钟磁盘缓存 + 10 分钟会话内存缓存），页面启动零行情请求；持仓/自选代码以「常用」快捷按钮一键填入。

```bash
streamlit run app.py   # 统一入口：投资组合 + K线 双页面
```

---

## 数据源与降级策略

行情路由优先级（可叠加启用）：

| 优先级 | 数据源 | 说明 |
|--------|--------|------|
| 1 | **IBKR**（TWS / IB Gateway） | 有订阅则为实时；无订阅则 delayed；一次 `reqTickers` 批量快照 |
| 2 | **OpenBB → yfinance** | 批量 quote（1 次请求）+ 失败逐个重试 |
| 3 | **akshare**（东财） | A股/港股 spot + 历史；大陆网络下可优先切 akshare |
| 4 | **CFETS / yfinance** | 汇率：CFETS 一次拿全 XXX/CNY；直对/逆对/USD 桥接兜底 |

- 页面侧栏可勾选「A股/港股优先 akshare」「🔗 IBKR 行情」
- 英股 GBp 便士报价自动换算为 GBP；汇率缓存 10 分钟，行情缓存 5 分钟
- IBKR 连接参数**从配置文件读取**（不写死在代码里）：优先 `ibkr.json`，其次 `ibkr.example.json` 模板兜底，也可用环境变量 `IBKR_CONFIG=/path/to/xxx.json` 指定；交易所映射在同一文件（A股默认 `SEHK`，B股可配置 `SHSE`/`SZSE`，因沪深港通合约挂在 HKEX 下，需配 `tradingClass`）
- IBKR 断开时自动静默回退下一级数据源，不影响页面运行；持仓同步见下方

---

## 配置 IBKR 连接

```bash
# 复制模板为真实配置，再按需修改（真实配置已被 .gitignore 忽略，不会误提交）
cp ibkr.example.json ibkr.json
```

- `ibkr.json` 含连接参数与**交易所映射**（`exchanges`）。Gateway 模式：`"mode": "paper"` 模拟盘（4002）/ `"live"` 实盘（4001），默认 paper；写 `"port"` 可显式指定任意端口（如 TWS 7496/7497）。环境变量 `IBKR_MODE=live` 可临时覆盖
- 配置文件缺字段时自动继承模板/兜底值；`_comment` 开头的字段会被忽略
- 不想在项目目录放配置文件时，可用环境变量指向其他路径：

```bash
IBKR_CONFIG=/data/my-ibkr.json python -m tracker.cli snapshot --ibkr
```

---

## 从 IBKR 同步真实持仓

```bash
python -m tracker.cli sync --dry-run          # 预览，仅打印不写入
python -m tracker.cli sync --mode live        # 实盘模式写入 portfolio.json（自动备份 .bak）
```

- 自动将 IBKR 账户股票持仓转换为 Yahoo 代码并写入 `portfolio.json`
- 保留原有 `base_currency`；`avg_cost` 取自 IBKR（合约货币每股均价，含佣金）
- 无法映射为 Yahoo 代码的标的（权证/期权/基金等）会跳过并在控制台提示
- 反向映射规则：`SEHK + CNY → .SS/.SZ`；`SHSE + USD → .SS (B股)`；`SZSE + HKD → .SZ (B股)`；`SEHK + HKD → .HK`；`IBIS/FWB + EUR → .DE`；`LSE + GBP → .L`；`TSE + CAD → .TO`；`ASX + AUD → .AX`；`SMART + USD → 原码`

---

## 已知限制

- 免费数据源：非美股行情普遍延迟 15–30 分钟；无 SLA，偶发限流（已内置批量请求 + 重试缓解）
- akshare 东财 spot 接口对部分数据中心 IP 不友好（本项目所在机器即如此），此时自动回落 Yahoo
- CFETS 汇率为中间价，与离岸 CNH 有细微差异，组合展示场景可忽略
- `今日估算` 按各持仓 `涨跌幅 × 当前市值` 近似，非精确日内盯市
- IBKR 行情需本机运行 IB Gateway 且 API 已启用；A股/港股/B股数据若无市场数据订阅，`reqTickers` 可能返回空值，自动回退 Yahoo/akshare
- IBKR A股合约默认映射为 `SEHK/CNY`，若你的账户显示不同交易所代码，在 `ibkr.json` 中修改 `exchanges.CN`；B股合约根据 IBKR 返回的 `SHSE/USD` 或 `SZSE/HKD` 自动识别

---

## Roadmap

- [x] ~~IBKR 行情接入 + 持仓同步~~（已完成）
- [ ] 交易流水记录与分批成本（FIFO）
- [ ] 分红/拆分事件跟踪
- [ ] EODHD 付费 provider 接入（升级数据质量）
- [ ] 历史净值曲线（按日聚合组合市值）

---

## OpenBB Portfolio Tracker (English)

> **Local-first multi-market portfolio tracker. Zero API keys. Completely free.**

A **local-first** portfolio tracker supporting **A-shares / B-shares / HK / US / DE / GB / CA / AU** markets. Built with Streamlit, OpenBB (yfinance), and akshare.

### Features

- **Multi-currency portfolio tracking** — automatic FX conversion, unified base currency
- **Watchlist with price threshold alerts** — two-level upper/lower thresholds, severity levels, distance prediction
- **IBKR integration** — TWS / IB Gateway real-time snapshots with automatic fallback
- **K-line (candlestick) charts** — daily/weekly/monthly, MA overlays, volume, interactive HTML
- **Streamlit dashboard** — holdings detail, asset allocation pie charts, trend comparison, watchlist alerts
- **Unified CLI** — snapshot, quote, FX, history, kline, sync, cache; all commands support `--json`

### Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start Streamlit dashboard
streamlit run app.py

# CLI examples
python -m tracker.cli snapshot
python -m tracker.cli quote AAPL 600519.SS
python -m tracker.cli fx USD CNY EUR
python -m tracker.cli kline AAPL --months 12
python -m tracker.cli sync --dry-run
```

### Data Source Fallback

Quotes are fetched with a priority chain:

1. **IBKR** (TWS / IB Gateway) — real-time if subscribed, delayed otherwise, batch snapshot
2. **OpenBB → yfinance** — batch quote (1 request) + individual retry
3. **akshare** (East Money) — A-shares / HK spot + history
4. **CFETS / yfinance** — FX: CFETS for full XXX/CNY table, direct/inverse/USD bridge fallback

IBKR disconnects silently fall back to the next source; the page continues running without interruption.

### Project Structure

```
tracker/           Core Python package (CLI, pricing, FX, analytics, watchlist, IBKR, caching, charting)
app.py             Streamlit dashboard (portfolio + K-line)
kline_page.py      K-line page logic
portfolio.json     Holdings config (editable via page)
watchlist.json     Watchlist config (editable via page)
ibkr.json          IBKR config (gitignored, copy from ibkr.example.json)
tests/             Unit tests (offline, mocked)
```

### License

MIT
