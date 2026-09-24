# OpenBB Portfolio Tracker

> 本地多市场投资组合追踪：人类用 Streamlit 页面，Agent 用 CLI（`--json`），零 API Key，完全免费

[中文](#openbb-portfolio-tracker) · [English](#openbb-portfolio-tracker-en)

---

## 项目目标

1. **投资组合追踪** — 多数据源接入 A股 / B股 / 港股 / 美股 / 德股 / 英股 / 加股 / 澳股 / 加密货币（规划：新加坡股 `.SI`）
2. **Watchlist 管理** — 多列表归属 + 两级价格阈值提醒
3. **按代码或名称查询** — 模糊搜索代码/名称，一键查看 K 线与基本数据
4. **双前端** — 人类用 Streamlit UI；Agent（hermes / openclaw 等）用统一 CLI，全部子命令支持 `--json`，解决手机上查看的问题
5. **只读券商对接** — IBKR Gateway（TWS/IB Gateway）行情 + 持仓同步；A股券商持仓文件导入；Hyperliquid 行情源（规划：HL/A股券商实时持仓对接）
6. **K 线对比与比值** — 多股同坐标系对比（归一化起点=100，已实现）；两标的收盘价比值画成一根 K 线（规划）

---

## 简介

OpenBB Portfolio Tracker 是一款**本地优先**的投资组合追踪工具，提供：

- **多币种持仓追踪** — 自动汇率换算，统一基础货币展示
- **自选股价格阈值提醒** — 两级上限/下限，触发分级，距离预测
- **IBKR 行情接入** — TWS / IB Gateway 实时快照，自动回退
- **K 线蜡烛图** — 日K / 周K / 月K，MA 均线，成交量，MACD / RSI / KDJ / 布林带，滑动模式默认加载上市以来全量历史
- **宏观/风险指数K线** — 独立 IX.<KEY> 代码规范（美元指数 / VIX 恐慌指数 / 沪深 300 等），独立页面与 `index-kline` 子命令，独立 provider 源链
- **多股对比** — 任意代码同坐标系折线对比，归一化（起点=100）跨币种跨量级比较
- **Streamlit 可视化页面** — 组合明细 / 资产配置 / 自选提醒 / K线查询（含多股走势对比）/ 指数K线 / 设置
- **统一 CLI** — 13 个子命令（快照、报价、持仓、自选、汇率、历史、K线、指数K线、导入、同步、缓存…），全部 `--json` 输出
- **链上钱包导入** — EVM 五链地址余额只读查询，一键写入持仓
- **A股券商持仓导入** — 解析券商客户端导出的 CSV/Excel 持仓文件

> **核心设计**：数据层抽象为 **provider 层**（全球股票 / A股·B股 / 加密货币三域，后缀即域、互不冲突），
> 多级降级（IBKR → yfinance → akshare / Binance → Hyperliquid），离线可用的 SQLite 磁盘缓存，
> 所有配置为可编辑 JSON 文件，不依赖任何云服务。

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
streamlit run tracker/ui/app.py
```

页面默认运行在 `http://localhost:8501`，侧边栏五个页面：

- **组合** — 持仓编辑、明细表、资产配置饼图、自选提醒
- **K线** — 任意代码 K 线查询（支持按代码或名称模糊搜索）+ 多股走势对比（K线子功能）
- **指数K线** — 宏观/风险指数查询（IX.<KEY> 分组下拉：风险波动 / 美元利率 / 美股 / 全球 / 中国）
- **导入** — IBKR 账户 / 链上钱包 / A股券商文件导入
- **设置** — 涨跌配色（红涨绿跌/绿涨红跌）、数据源偏好（akshare / IBKR）、基础货币

### CLI 快速上手

```bash
# 查看持仓 + 自选 + 阈值提醒快照
python -m tracker.cli snapshot

# 切换基础货币（如 USD）
python -m tracker.cli snapshot --base USD

# 只看某个自选列表
python -m tracker.cli snapshot --watchlist 科技

# JSON 输出（机器可读，适合脚本/Agent）
python -m tracker.cli snapshot --json

# 查询单个/多个代码实时行情
python -m tracker.cli quote AAPL 600519.SS BTC-USD

# 汇率查询
python -m tracker.cli fx USD CNY EUR

# 近 12 个月历史价格
python -m tracker.cli history AAPL --months 12

# 生成 K 线蜡烛图（交互式 HTML）

# 生成宏观/风险指数K线（独立于股票 K 线）
python -m tracker.cli index-kline IX.DXY --months 12      # 美元指数
python -m tracker.cli index-kline IX.VIX --period weekly  # 恐慌指数 周K
python -m tracker.cli index-kline IX.CSI300 --list        # 查看全部已收录指数
python -m tracker.cli kline AAPL --months 12
python -m tracker.cli kline 600519.SS --ma 5,10,20,60
python -m tracker.cli kline 0700.HK --period weekly --open

# 持仓管理
python -m tracker.cli portfolio list
python -m tracker.cli portfolio add AAPL --quantity 10 --avg-cost 180
python -m tracker.cli portfolio remove AAPL
python -m tracker.cli portfolio set-base USD

# 自选股管理
python -m tracker.cli watchlist list
python -m tracker.cli watchlist add NVDA --list 科技,美股 --upper1 260
python -m tracker.cli watchlist remove NVDA

# 导出自选监控报告
python -m tracker.cli report
python -m tracker.cli report -f json -o report.json
python -m tracker.cli report -f csv -w 科技

# 导出组合快照报表（CSV / JSON / Markdown）
python -m tracker.cli export -f md -o snapshot.md
python -m tracker.cli export -f csv -o holdings.csv

# 从链上地址查询加密资产余额（只读，支持 eth/bsc/polygon/arbitrum/avalanche）
python -m tracker.cli import wallet eth 0xd8dA...6045 --dry-run  # 仅查询
python -m tracker.cli import wallet eth 0x...                    # 追加写入 portfolio.json

# 行情磁盘缓存管理
python -m tracker.cli cache info
python -m tracker.cli cache clear

# 统一导入（追加合并为默认，--overwrite 覆盖全部持仓，写前自动备份 .bak）
python -m tracker.cli import ibkr --dry-run            # 预览 IBKR 账户持仓（需 Gateway 已登录）
python -m tracker.cli import ibkr --mode live --overwrite
python -m tracker.cli import wallet eth 0xd8dA...      # 链上钱包余额追加导入
python -m tracker.cli import file 持仓.csv --dry-run   # 券商导出 CSV/Excel 解析预览
# 兼容入口: sync / import-wallet / python -m tracker.ibkr_sync|ashare_sync 仍可用

# 运行单元测试（不联网，纯逻辑 mock）
python -m pytest tests/ -q
```

> 完整 CLI 帮助：`python -m tracker.cli --help`（或每个子命令 `--help`）。`python -m tracker` 等价。

---

## 供 Agent 接入的 CLI 能力

所有子命令均支持 `--json` 输出机器可读结果，专为 hermes / openclaw 之类手机 Agent 设计：

```bash
python -m tracker.cli snapshot --json          # 持仓 + 自选 + 阈值触发，一次拿全
python -m tracker.cli quote 0700.HK --json     # 单/多代码实时行情
python -m tracker.cli watchlist list --json    # 自选 + 行情 + 触发状态
python -m tracker.cli kline AAPL --json        # K 线数据（不生成图表）
python -m tracker.cli index-kline IX.DXY --json # 指数K线数据 (IX.<KEY>, 不生成图表)
python -m tracker.cli export -f json           # 完整快照 JSON
```

- 无交互、无 TUI 依赖，纯 stdout；错误走 stderr 并以非零码退出
- 汇总/明细一次请求返回，适合小上下文 Agent 直接消费

---

## 代码规范（自有后缀规范 + 内部 type 字段）

后缀规范是**本项目自定义**的（恰好与 Yahoo Finance 兼容）；域的权威判据是系统内部维护的 **`type` 字段**
（`global`/`cn`/`crypto`），在 portfolio/watchlist 保存与 IBKR/A股/钱包导入时自动推导覆写，用户不可见。

| 市场 | 后缀 | 示例 | 币种 | type | 说明 |
|------|------|------|------|------|------|
| 美股 | 无 | `AAPL` | USD | global | |
| A股 | `.SS` / `.SZ` | `600519.SS` `000001.SZ` | CNY | cn | 也接受 `.SH` |
| B股 | `.SS` / `.SZ` | `900902.SS` `200012.SZ` | USD / HKD | cn | 上海 B 股 `9` 开头以**美元**交易，深圳 B 股 `2` 开头以**港币**交易 |
| 北交所 | `.BJ` | `830799.BJ` | CNY | cn | |
| 港股 | `.HK` | `0700.HK` `0941.HK` | HKD | global | **4 位补零**；`00700.HK` 自动归一 |
| 德股 | `.DE` 等 | `SAP.DE` | EUR | global | `.F` `.BE` `.DU` `.HM` `.SG` `.MU` 均可 |
| 英股 | `.L` | `BP.L` | GBP | global | Yahoo 报价单位为便士（GBp），系统自动 ÷100 换算为英镑；`.IL` `.AL` 同属伦交所 |
| 加股 | `.TO` 等 | `RY.TO` | CAD | global | `.V`（TSXV）`.CN` `.NE` 均可 |
| 澳股 | `.AX` | `BHP.AX` | AUD | global | |
| 加密货币 | `-QUOTE` | `BTC-USD` `ETH-USDT` | 计价货币 | crypto | 连字符 `BASE-QUOTE`（Yahoo crypto 规范）；无分隔符 `BTCUSD` 自动补全 |

**互斥保证**：点后缀/裸代码永远是股票，连字符+计价货币永远是加密货币（单字母连字符 `BRK-B`
是美股类别股，不是 crypto）。推导唯一入口 `symbols.type_for_symbol()`；测试用例、搜索结果、
IBKR/A股/钱包导入产出的代码均符合该规范。

`avg_cost`（成本）按**当地货币**填写；英股填**英镑**（如 `4.30 = £4.30`，不是便士）；
B 股按**实际交易币种**填写（沪 B 美元 / 深 B 港币，与行情币种一致）。

---

## K 线查询（按代码或名称）

K线页支持两种输入方式：

- **代码直查** — `AAPL` / `600519.SS` / `0700.HK` / `SAP.DE` / `BP.L` / `BTC-USD`
- **名称模糊搜索（分域）** — 输入代码或名称片段（如「茅台」「Tencent」「BTC」「bitcoin」），每个 provider 提供自己的搜索接口，UI 按「A股/北交所 · 美股/港股 · 加密货币」三块下拉分组展示，互不混排；美股/港股走 IBKR 合约匹配（Gateway 在线时优先）→ yfinance Search，A股/北交所走东财 suggest（中文名/拼音），加密货币走 yfinance Search（返回即 Yahoo 规范代码 BTC-USD）；纯在线搜索接口封装 + 归一化，无本地目录、无缓存快照，接口失败即空结果 + 合法代码直查

页面端基于 TradingView 开源的 **lightweight-charts** 渲染（已 vendored，无新增依赖），券商 App 通用交互：

- **蜡烛图 + 成交量副图 + MA 均线**（默认 MA5/20/60，可自定义），默认红涨绿跌（设置页可切国际配色）
- **技术指标** — MACD / RSI / KDJ / 布林带，多选叠加，周期参数可调
- **拖动 = 平移**（带惯性滚动），**滚轮/捏合 = 缩放时间轴**，触控板双指横滑平移、双指纵向/捏合缩放
- **滑动模式一次加载上市以来全量历史** — 图表内拖动/缩放全程纯前端，不触发取数；另有「范围」模式按月窗口兜底
- 十字光标 + 顶部 OHLC/涨跌/量/均线信息栏；价格轴随可见区间自动缩放；副图分隔线可拖拽；双击轴复位
- **非交易日断轴**：周末/节假日不出空隙，图形连续
- **日K / 周K / 月K** 一键切换（周K 按 W-FRI 对齐）
- 数据经过**清洗校验**（去重/排序，丢弃 high<low 等自相矛盾的脏行），英股便士自动换算
- **双层缓存**：SQLite 磁盘缓存 30 分钟（`cache info` 可查看），页面另有内存缓存；CLI `--refresh` 强制刷新

CLI 导出的独立 HTML 用 **plotly**：

```bash
python -m tracker.cli kline AAPL --months 12            # 生成 var/kline_AAPL.html
python -m tracker.cli kline 600519.SS --ma 5,10,20,60   # 自定义均线
python -m tracker.cli kline 0700.HK --period weekly --open  # 周K + 自动打开浏览器
```

---

## 多股对比

「K线」页的「走势对比」子 tab：任意多只代码（持仓/自选点选，或直接输入任意代码）画进**同一坐标系**：

- 每只代码一条收盘价折线；不同市场按各自交易日绘制
- **归一化模式（默认）**：各代码按自身区间首个收盘归一化为 100 起点，跨币种/跨量级可比
  （如对比腾讯与南非报业、A股与美股的相对走势）
- 图下方给出各代码**区间涨跌**；复用 K线组件的拖动/缩放/十字光标交互
- 日K / 周K / 月K 周期可切

> K 线**比值**模式（两标的收盘价相除画成一根 K 线，如 `0700.HK / NPN.L`）在 Roadmap 中，尚未实现。

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
tracker/ui/           Streamlit 页面包（streamlit run tracker/ui/app.py）
├── app.py              主页（组合：持仓编辑/明细/配置/自选提醒）
├── kline_page.py       「K线」页面（搜索框 + lightweight-charts 组件 + 上市以来全量滑动 + 多股走势对比）
├── index_page.py       「指数K线」页面（IX.<KEY> 分组下拉, 独立于股票 K线页）
├── import_page.py      「导入」页面（IBKR 账户 / 链上钱包 / 券商文件, 追加合并或覆盖）
├── settings_page.py    「设置」页面（配色 / 数据源 / 基础货币）
└── settings.py         settings.json 读写（涨跌配色 / prefer_akshare / use_ibkr）+ portfolio 读写助手
tracker/
├── __main__.py         python -m tracker 入口（转发到 cli）
├── cli/                CLI 包（python -m tracker.cli，13 个子命令各一个模块）
│   ├── __init__.py       argparse 注册与分发
│   ├── snapshot.py       快照（持仓 + 自选 + 阈值触发）
│   ├── index_kline.py    指数K线 HTML + 摘要（IX.<KEY>, --json / --list）
│   ├── quote.py          实时行情
│   ├── portfolio.py      持仓管理（list/add/remove/set-base）
│   ├── watchlist.py      自选管理（list/add/remove）与阈值提醒
│   ├── report.py         自选监控报告（md/csv/json）
│   ├── export.py         组合快照导出（csv/json/md）
│   ├── fx.py             汇率
│   ├── history.py        历史价格
│   ├── kline.py          K线 HTML + 摘要（--json 输出数据）
│   ├── importer.py       统一导入（import ibkr / wallet / file, 追加/覆盖）
│   ├── sync.py           IBKR 持仓同步（兼容入口 → import ibkr）
│   ├── wallet.py         链上钱包导入（兼容入口 → import wallet）
│   └── cache.py          磁盘缓存管理
├── symbols.py          自有代码规范、市场域识别、type_for_symbol() 权威域推导（唯一入口）
├── providers/          **数据 provider 层**（按权威 type 域隔离）
│   ├── base.py         Provider 基类 + Quote/SymbolEntry 数据结构 + resolve(type) 域路由
│   ├── cn_stocks.py        A股/B股域（.SS/.SZ/.BJ）：akshare 固定优先, yfinance 兜底
│   ├── global_stocks.py    全球股票域（美股/港股/欧股…）：yfinance 主源, 港股 akshare 兜底; 搜索走 IBKR → yfinance Search
│   ├── crypto.py           加密货币域（BASE-QUOTE）：Binance → Hyperliquid(USD系) → yfinance
│   ├── index.py            指数域（IX.<KEY>）：中国指数 akshare 优先, 其余 yfinance 主源 + 新浪兜底
│   ├── orchestration.py    批量编排：按域分组取数 → 聚合 quotes/errors/notes
│   └── em_suggest.py       东财 suggest 搜索客户端（cn 域搜索底层 HTTP 封装 + 代码归一）
├── prices.py           行情门面（历史 API 保持不变, 全部路由到 providers）
├── search.py           搜索聚合层：search_grouped() 按域调 provider.search() 保持分组，本地目录（data/symbol_list.json）仅作各域离线兜底
├── fx.py               汇率：CFETS（akshare）优先，yfinance 货币对兜底（直对/逆对/USD 桥）
├── cache.py            行情 SQLite 磁盘缓存（实时 5 分钟 / K线 30 分钟）
├── charting.py         K线渲染（plotly CLI HTML + lightweight-charts 页面组件：蜡烛/均线/指标/对比）
├── analytics.py        组合视图与指标（纯函数）
├── watchlist.py        自选配置存储门面（读写委托 storage，仅 I/O 归一）
├── services/           **独立用例层**（用例 = 取数 providers/fx + 纯函数计算 + 组装视图）
│   ├── rules.py          阈值规则（纯函数：两级上下限状态判定 + 距离）
│   ├── watchlist.py      自选视图组装 + 取数用例（watchlist list / report 复用）
│   └── snapshot.py       快照用例（take_snapshot / snapshot_json, 存储剥离）
├── ibkr.py             IBKR 行情接入 + 持仓读取（可选依赖 ib_async，失败静默回退）
├── importer.py         **统一导入管道**：三来源采集 → 追加/覆盖合并 → 备份写盘
├── ibkr_sync.py        IBKR 持仓导入兼容入口（委托 importer，等价 import ibkr）
├── ashare_sync.py      A股券商文件解析 + 兼容入口（等价 import file）
└── wallet.py           链上钱包余额查询（EVM 五链, 轻钱包 RPC, 只读）
portfolio.json          持仓配置（页面可直接编辑保存; type 字段由系统自动维护, 手改无效;
                        用户本地数据, 已 gitignore, 模板 portfolio.example.json）
watchlist.json          自选股配置（页面可直接编辑保存; type 字段由系统自动维护, 手改无效;
                        用户本地数据, 已 gitignore, 模板 watchlist.example.json）
settings.json           显示与数据源设置（配色 / prefer_akshare / use_ibkr;
                        已 gitignore, 模板 settings.example.json）
*.example.json          各配置模板（随仓库提交: 首次使用 cp <名>.example.json <名>.json）
ibkr.example.json       IBKR 配置模板（随仓库提交）
ibkr.json               IBKR 真实配置（已 gitignore，不随仓库提交）
var/                    运行时生成产物（行情缓存 quotes_cache.db、kline/compare 图表 HTML）,
                        已 gitignore, 可随时整目录删除（重建即自动再生成）
docs/                   研究笔记（如 A股券商导入方案调研）
tests/                  单元测试（离线, mock）
```

---

## 数据源与降级策略 (provider 层)

数据层按市场域拆分为三个 provider（`tracker/providers/`），只在组合/watchlist 聚合。
路由依据是系统内部维护的权威 `type` 字段（用户不可见），后缀规范仅用于推导 type：

| type | 覆盖代码 | 实时行情优先级 | 历史K线优先级 |
|----------|----------|----------------|----------------|
| **global** | 裸代码（美股）、`.HK/.DE/.L/.TO/.AX…` | IBKR(可选) → yfinance 批量 → 港股 akshare | IBKR(可选) → yfinance（港股可 `--akshare` 翻转） |
| **cn** | `.SS/.SZ/.BJ`（A/B股、北交所） | **akshare 优先** → yfinance | **akshare 优先** → yfinance |
| **crypto** | `BASE-QUOTE`（`BTC-USD`） | **Binance API** → Hyperliquid 永续（仅USD系） → yfinance | **Binance klines** → Hyperliquid → yfinance |
| **index** | `IX.<KEY>`（`IX.DXY` 美元指数 / `IX.VIX` 恐慌指数 / `IX.CSI300` 沪深 300 …） | —（指数无实时行情, 不参与持仓聚合） | 中国指数 **akshare 优先** → yfinance；其余 yfinance → akshare 新浪兜底 |

- **type 即域**：`type` 字段由系统在保存/导入时按 `symbols.type_for_symbol()` 自动推导覆写，
  手改 JSON 无效。点后缀/裸代码永远是股票，连字符+计价货币永远是加密货币，同一代码不会映射两个域。
  测试用例、搜索结果、导入产出的代码均符合该规范。
- 汇率：CFETS 一次拿全 XXX/CNY；直对/逆对/USD 桥接兜底
- `--akshare` 开关仅影响港股数据源顺序（A股域固定 akshare 优先）；设置页同名开关全局生效
- IBKR 行情可选叠加于任何域（需订阅）；英股 GBp 便士报价自动换算为 GBP；汇率缓存 10 分钟，行情缓存 5 分钟
- IBKR 连接参数**从配置文件读取**（不写死在代码里）：优先 `ibkr.json`，其次 `ibkr.example.json` 模板兜底，也可用环境变量 `IBKR_CONFIG=/path/to/xxx.json` 指定；交易所映射在同一文件（A股默认 `SEHK`，B股可配置 `SHSE`/`SZSE`，因沪深港通合约挂在 HKEX 下，需配 `tradingClass`）
- IBKR 断开时自动静默回退下一级数据源，不影响页面运行；持仓同步见下方
- Hyperliquid 永续价为 Binance 之后的第二加密源（markPx 与现货存在基差，仅 USD 系计价代码）

---

## 配置 IBKR 连接

- `ibkr.json` 含连接参数与**交易所映射**（`exchanges`）。Gateway 模式：`"mode": "paper"` 模拟盘（4002）/ `"live"` 实盘（4001），默认 paper；写 `"port"` 可显式指定任意端口（如 TWS 7496/7497）。环境变量 `IBKR_MODE=live` 可临时覆盖
- 配置文件缺字段时自动继承模板/兜底值；`_comment` 开头的字段会被忽略
- 不想在项目目录放配置文件时，可用环境变量指向其他路径：

```bash
IBKR_CONFIG=/data/my-ibkr.json python -m tracker.cli snapshot --ibkr
```

---

## 持仓导入（IBKR / 链上钱包 / 券商文件）

三种来源统一走 `tracker.importer` 管道：CLI `import` 子命令与 Streamlit 侧边栏「导入」页
（三 tab 各自独立导入）调用同一套采集 + 合并 + 写盘逻辑。

**导入方式**（CLI 与页面一致）：

- **追加合并**（默认）：按代码更新 `quantity`/`avg_cost`，新代码追加，其余持仓保留。
  钱包导入额外带来源标记（链 + 地址）：同一地址重复导入仍是覆盖（幂等），
  不同地址/链的同名代币（如 eth 与 bsc 上的 `USDT-USD`）会累加，不会互相覆盖
- **覆盖**（`--overwrite`）：清空现有持仓后重写

写盘前自动备份 `portfolio.json.bak`；保留 `base_currency` 与文件中的其它自定义键；
写盘为原子操作（临时文件 + `os.replace`），中断不会留下半截 JSON；若 `portfolio.json` /
`watchlist.json` 损坏，读取时自动尝试同名 `.bak` 恢复，仍失败则报可读错误退出（不会静默清空数据）。
`--dry-run` 仅预览不写入；采集结果为空时即使覆盖模式也不会清空文件。

### IBKR 账户

```bash
python -m tracker.cli import ibkr --dry-run          # 预览，仅打印不写入
python -m tracker.cli import ibkr --mode live        # 实盘模式追加导入（自动备份 .bak）
python -m tracker.cli import ibkr --overwrite        # 覆盖全部持仓
```

- 自动将 IBKR 账户股票持仓转换为规范代码并写入 `portfolio.json`
- 保留原有 `base_currency`；`avg_cost` 取自 IBKR（合约货币每股均价，含佣金）
- 无法映射为规范代码的标的（权证/期权/基金等）会跳过并在控制台提示
- 反向映射规则：`SEHK + CNY → .SS/.SZ`；`SHSE + USD → .SS (B股)`；`SZSE + HKD → .SZ (B股)`；`SEHK + HKD → .HK`；`IBIS/FWB + EUR → .DE`；`LSE + GBP → .L`；`TSE + CAD → .TO`；`TSXV + CAD → .V`；`CSE + CAD → .CN`；`NEOEX + CAD → .NE`；`ASX + AUD → .AX`；`SMART + USD → 原码`
- 兼容入口：`python -m tracker.cli sync` / `python -m tracker.ibkr_sync`（默认覆盖，加 `--append` 追加）

### A股券商文件导入

A股券商无跨券商统一 API（监管对个人程序化接入持续收紧），采用**文件导入**模式：
在券商客户端（同花顺/通达信/华泰/东财/QMT 等）手动导出持仓为 CSV/Excel，本工具解析后映射为 `.SS/.SZ/.BJ` 代码写入 `portfolio.json`。零凭证、跨平台、自动备份。

```bash
python -m tracker.cli import file 持仓.csv --dry-run   # 预览
python -m tracker.cli import file 持仓.xlsx            # 追加导入（自动备份 .bak）
python -m tracker.cli import file 持仓.csv --overwrite --json
```

- 列名模糊匹配（代码/数量/成本列，各家券商叫法不同也能识别）；CSV 自动尝试 utf-8/gbk/gb18030 编码
- 仅识别 6 位 A股代码，非 A股行跳过并提示
- 兼容入口：`python -m tracker.ashare_sync 持仓.csv`（默认覆盖，加 `--append` 追加）
- 页面端：「导入」页 → 「券商文件」tab 上传 CSV/Excel 预览后导入

### 链上钱包导入（加密资产）

只读查询 EVM 链上地址余额并写入 `portfolio.json`，无需 API key、不接触私钥：

```bash
python -m tracker.cli import wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 --dry-run
python -m tracker.cli import wallet bsc 0x... --base-currency USDT --json
python -m tracker.cli import wallet polygon 0x... --tokenlist my_tokens.json
```

- 支持链：`eth` / `bsc` / `polygon` / `arbitrum` / `avalanche`（每链多个公共 RPC 自动切换）
- 轻钱包策略：主币 `eth_getBalance` + ERC-20 `balanceOf` 批量调用；内置主流代币 tokenlist，可用外部 JSON 覆盖
- 不依赖 web3.py，直接 HTTP JSON-RPC
- 兼容入口：`python -m tracker.cli import-wallet`（`--add` 追加写入 / `--overwrite` 覆盖）

---

## Agent / CLI 集成

本仓库的双前端设计：Streamlit 给人看，CLI 给 Agent 用。hermes / openclaw 等手机 Agent 只需执行
`python -m tracker.cli <子命令> --json` 即可获取与页面完全一致的数据（持仓/自选/行情/K线/汇率/导出），
无需打开浏览器。建议 Agent 默认使用 `snapshot --json` 作为「现在怎么样了」的一站式入口。

---

## 已知限制

- 免费数据源：非美股行情普遍延迟 15–30 分钟；无 SLA，偶发限流（已内置批量请求 + 重试缓解）
- akshare 东财 spot 接口对部分数据中心 IP 不友好（本项目所在机器即如此），此时自动回落 Yahoo
- 搜索全部为在线接口（美股/港股：IBKR→yfinance Search；A股/北交所：东财 suggest；crypto：yfinance Search）；中文查询仅东财支持，美股/港股/crypto 搜中文名无结果；接口失败即该域空结果 + 合法代码直查
- CFETS 汇率为中间价，与离岸 CNH 有细微差异，组合展示场景可忽略
- `今日估算` 按各持仓 `涨跌幅 × 当前市值` 近似，非精确日内盯市
- IBKR 行情需本机运行 IB Gateway 且 API 已启用；A股/港股/B股数据若无市场数据订阅，`reqTickers` 可能返回空值，自动回退 Yahoo/akshare
- IBKR A股合约默认映射为 `SEHK/CNY`，若你的账户显示不同交易所代码，在 `ibkr.json` 中修改 `exchanges.CN`；B股合约根据 IBKR 返回的 `SHSE/USD` 或 `SZSE/HKD` 自动识别
- 新加坡股（`.SI`）暂未支持

---

## Roadmap

按项目目标排序：

- [x] IBKR 行情接入 + 持仓同步（只读）
- [x] A股券商持仓文件导入（CSV/Excel）
- [x] 链上钱包余额导入（EVM 五链）
- [x] 多股 K 线对比（同坐标系 + 归一化）
- [ ] **K 线比值模式** — 两标的收盘价比值画成 K 线（如腾讯 / 南非报业）
- [ ] **新加坡股接入**（`.SI`，Yahoo 后缀即可覆盖行情，需补目录与测试）
- [ ] Hyperliquid / A股券商实时**持仓**对接（当前 HL 仅为行情源）
- [ ] 交易流水记录与分批成本（FIFO）
- [ ] 分红/拆分事件跟踪
- [ ] 历史净值曲线（按日聚合组合市值）
- [ ] EODHD 付费 provider 接入（升级数据质量）

---

## OpenBB Portfolio Tracker (English)

> **Local-first multi-market portfolio tracker. Human-facing Streamlit UI + agent-facing CLI (`--json`). Zero API keys. Completely free.**

A **local-first** portfolio tracker supporting **A-shares / B-shares / Beijing SE / HK / US / DE / GB / CA / AU** and **crypto** (SGX planned). Data layer is split into three isolated **providers** (global stocks / CN stocks / crypto) that only meet at the portfolio & watchlist aggregation level.

### Goals

1. Multi-source portfolio tracking (A/B/CN-HK/US/DE/GB/CA/AU + crypto; SGX planned)
2. Watchlist management with two-level price-threshold alerts
3. Look up K-line & fundamentals by symbol **or name** (fuzzy search)
4. Dual frontends — Streamlit for humans, unified JSON CLI for agents (hermes / openclaw)
5. Read-only broker integration — IBKR Gateway, A-share broker file import, Hyperliquid quotes
6. K-line comparison (normalized overlay, done) and **ratio mode** (planned)

### Features

- **Multi-currency portfolio tracking** — automatic FX conversion, unified base currency
- **Watchlist with price threshold alerts** — two-level upper/lower thresholds, severity levels, distance prediction
- **IBKR integration** — TWS / IB Gateway snapshots + position sync, automatic fallback
- **K-line charts** — daily/weekly/monthly, MA overlays, volume, MACD/RSI/KDJ/Bollinger; slide mode loads full listing history by default
- **Macro/risk index K-lines** — dedicated `IX.<KEY>` symbol space (USD index, VIX, CSI 300, ...), separate page & `index-kline` subcommand, dedicated provider source chain
- **Multi-symbol comparison** — same-coordinate overlay, normalized to 100
- **Unified import pipeline** — IBKR account / EVM wallet (5 chains) / A-share broker CSV-Excel file, each with append-merge or overwrite mode (auto .bak backup); same logic drives the Streamlit「导入」page
- **Unified CLI** — 13 subcommands (snapshot, quote, portfolio, watchlist, report, export, fx, history, kline, import, sync, import-wallet, cache); all support `--json`

### Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Streamlit dashboard
streamlit run tracker/ui/app.py

# CLI examples
python -m tracker.cli snapshot --json
python -m tracker.cli quote AAPL 600519.SS BTC-USD
python -m tracker.cli kline AAPL --months 12
python -m tracker.cli import ibkr --dry-run
python -m tracker.cli import file positions.csv --dry-run
```

### Data Source Fallback

Quotes are fetched with a priority chain:

1. **IBKR** (TWS / IB Gateway) — real-time if subscribed, delayed otherwise, batch snapshot
2. **OpenBB → yfinance** — batch quote (1 request) + individual retry
3. **akshare** (East Money) — A-shares / HK spot + history (CN domain: akshare first)
4. **FX** — CFETS for full XXX/CNY table, direct/inverse/USD bridge fallback
5. **Indices** — `IX.<KEY>` historical only: CN indices via akshare (Sina), global via yfinance with Sina fallback

IBKR disconnects silently fall back to the next source; the page continues running without interruption.

### Project Structure

```
tracker/ui/        Streamlit dashboard (app.py + kline/index/import/settings pages + settings)
tracker/           Core package: cli/ subcommands, providers/, symbols, prices, fx,
                   search, cache, charting, analytics, watchlist, snapshot,
                   importer, ibkr, ibkr_sync, ashare_sync, wallet
portfolio.json     Holdings config (editable via page)
watchlist.json     Watchlist config (editable via page)
settings.json      Display & data-source settings
ibkr.json          IBKR config (gitignored, copy from ibkr.example.json)
tests/             Unit tests (offline, mocked)
```

### License

MIT
