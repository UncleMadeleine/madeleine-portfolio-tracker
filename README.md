# Madeleine Portfolio Tracker

> 本地多市场投资组合追踪：人类用 Streamlit 页面，Agent 用 CLI（`--json`），零 API Key，完全免费

[中文](#madeleine-portfolio-tracker) · [English](#madeleine-portfolio-tracker-english)

---

## 项目目标

1. **投资组合追踪** — 多数据源接入 A股 / B股 / 北交所 / 港股 / 美股 / 德股 / 英股 / 加股 / 澳股 / 日股 / 韩股 / 新加坡股 / 巴西股 / 加密货币
2. **Watchlist 管理** — 多列表归属 + 两级阈值提醒 (价格 / 市净率 PB / 总市值 mcap 三指标)
3. **按代码或名称查询** — 模糊搜索代码/名称，一键查看 K 线与基本数据
4. **双前端** — 人类用 Streamlit UI；Agent（hermes / openclaw 等）用统一 CLI，全部子命令支持 `--json`，解决手机上查看的问题
5. **只读券商对接** — IBKR Gateway（TWS/IB Gateway）行情 + 持仓同步；长桥 (LongPort) 行情/持仓（OAuth 登录）；A股券商持仓文件导入；Hyperliquid 永续行情源（规划：HL/A股券商实时持仓对接）
6. **K 线对比与比值** — 多股同坐标系对比（归一化起点=100，已实现）；两标的收盘价比值画成一根 K 线（规划）

---

## 简介

Madeleine Portfolio Tracker 是一款**本地优先**的投资组合追踪工具，提供：

- **多币种持仓追踪** — 自动汇率换算，统一基础货币展示
- **自选股阈值提醒** — 价格 / 市净率 (PB) / 总市值 (mcap) 三指标基准，两级上限/下限，触发分级，距离预测
- **长桥 (LongPort) 行情接入** — 可选行情/历史/持仓源（OAuth 登录），自带 PB (calc_indexes 实时口径) 反推每股净资产，失败自动回退默认数据源
- **EODHD 付费源 (BYOK)** — 自备 token 的增强源：历史 K 线默认在 yfinance 之前（拆股+分红复权），实时行情默认仅在默认源失败时兜底（`quotes=primary` 时走 `s=` 批量，每批 15 个代码）；PB/市值可用 EODHD fundamentals 补/替换 yfinance；LSE 便士折算、`.IL` 豁免、按交易所映射 EODHD ticker；未配置 token 时完全不影响免费链路
- **K 线蜡烛图** — 日K / 周K / 月K，MA 均线，成交量，MACD / RSI / KDJ / 布林带，滑动模式默认加载上市以来全量历史
- **宏观/风险指数K线** — 独立 IX.<KEY> 代码规范（美元指数 / VIX / 沪深 300 / 商品期货主力连续（黄金·白银·鸡蛋·豆粕·豆一·豆二·生猪·碳酸锂）/ 铀现货与长协价（Cameco 月度 U3O8）/ 水泥网建材指数 / 恒生官网港股指数（恒生指数·国企·恒生科技·红筹·恒生综合·恒指波幅 等共 12 个）/ 恒生AH股溢价指数 等，共 51 个，`index-kline --list` 可查全部），独立页面与 `index-kline` 子命令，独立 provider 源链
- **多股对比** — 任意代码同坐标系折线对比，归一化（起点=100）跨币种跨量级比较
- **财经日历** — 持仓/自选的本月与下月财报、预约披露、除权除息/派息事件（yfinance 单票 + akshare 整批，长桥可选兜底），独立页面与 `calendar` 子命令；支持导出 iCalendar (`.ics`) 到 Apple 日历 / Google Calendar / Outlook；A股只请求窗口内可能命中的报表期、按范围跳过无关板块，取数并发执行；首屏先展示磁盘缓存并后台刷新，服务进程内置保温线程按默认范围周期预热缓存，冷启动带 A股整批/海外单票进度条
- **首屏性能** — 组合与日历均采用 stale-while-revalidate：先渲染 30 分钟 / 7 天内的旧缓存秒开，后台静默重取；仅在完全无缓存时阻塞取数（组合/日历均显示实时进度条）；行情/汇率/日历均有进程级保温线程周期刷新磁盘缓存，且保温服务已抽成无 Streamlit 依赖的 `tracker/services/warm.py`，CLI 可用 `tracker warm` 一次性预热或 `--interval` 常驻保温
- **历史净值曲线** — 每日组合快照落独立 SQLite（`nav_history.db`，与行情缓存分离），组合页画总市值 + 成本走势；支持 `nav --save` 手动存档、组合页每次渲染自动 upsert、`nav --backfill` 按当前持仓 × 历史收盘价回填近似历史（含历史汇率折算）
- **链上钱包净值曲线** — `wallet-nav` / 「钱包净值」页按**链上转账记录回放历史余额**（不是「当前余额 × 历史价」），再按历史币价与历史汇率折算成每日净值；Ethereum / Avalanche / TRON / Solana 零 API Key，Polygon / Arbitrum / BSC 可配 Etherscan V2 key 或自定义浏览器
- **基本面分析** — 估值指标子视图：任意 A股/港股/美股的最新 PE(TTM)、PB、股息率、5 年平均股息率，以及历史 PE/PB 曲线与自身历史分位（百度股市通）；AH 溢价子视图保留单只 A+H 股票的溢价率曲线：A 股收盘 ÷ (H 股收盘 × 历史汇率) − 1，支持任意 A+H 股票对 / 范围切换，0% 同价参考线 + 区间高低点/均值/分位摘要（独立页面 `/fundamentals`）
- **数据健康观测** — provider 热路径记录每次真实回源的来源/成功失败/耗时（缓存命中不计）到 `health_events`；`tracker doctor` 与「数据健康」页聚合成功率、p50/p95、最近失败，并从持仓/自选视角列出**行情缺失/过期、持仓无 K线、汇率缺失**等「页面还能开但数字可能已经不准」的缺口；`doctor --probe` 可主动打一次真实行情/汇率；总体异常时退出码 1，可接告警脚本。事件保留 7 天 / 最多 2 万条，`cache clear` 不会误删排障线索
- **线上契约测试** — `tests/test_provider_contract_live.py`（`@pytest.mark.network`，默认不跑）真实调用各域代表代码，断言价格为有限正数、币种规范、历史日期升序/收盘为正、汇率为正；离线侧另有源链归名与坏数据拒绝测试，数据源改字段时能第一时间红
- **Streamlit 可视化页面** — 组合明细 / 资产配置 / 自选提醒 / K线查询（含多股走势对比）/ 指数K线 / 基本面分析 / 日历 / 钱包净值 / 数据健康 / 设置
- **统一 CLI** — 24 个子命令（快照、净值、钱包净值、报价、持仓、自选、监控报告、导出、汇率、历史、K线、日历、搜索、对比、指数K线、导入、长桥登录、EODHD 自检、同步、钱包导入、缓存管理、缓存保温 `warm`、数据健康 `doctor`、初始化），全部支持机器可读输出（`--json`，`report`/`export` 为 `-f json`）；`warm` 可一次性预热或 `--interval` 常驻，不依赖 Streamlit 进程
- **链上钱包导入** — EVM 五链 + TRON + Solana 共七条链地址余额只读查询，一键写入持仓
- **A股券商持仓导入** — 解析券商客户端导出的 CSV/Excel 持仓文件

> **核心设计**：数据层抽象为 **provider 层**（全球股票 / A股·B股 / 加密货币 / 指数四域，后缀即域、互不冲突），
> 多级降级（长桥 / IBKR → yfinance → akshare / Binance → Gate.io → Hyperliquid），离线可用的 SQLite 磁盘缓存，
> 所有配置为可编辑 JSON 文件，不依赖任何云服务。行情/历史取数经 **OpenBB** 平台统一封装
> （`openbb>=4.7.0`，provider=yfinance/akshare），零 API Key。

---

## 快速开始

### 环境要求

- Python ≥ 3.10
- pip / venv（或 pipx / Docker，见下方「打包与分发」）

### 安装

**方式一：pip 安装（推荐，装完即有 `tracker` / `tracker-ui` 命令）**

```bash
pip install madeleine-tracker          # 核心: CLI + 取数 (openbb/akshare/pandas)
pip install "madeleine-tracker[ui]"    # + Streamlit 页面 / plotly 图表
pip install "madeleine-tracker[all]"   # + IBKR / 长桥 可选增强
# 或 pipx (自动隔离环境, 命令全局可用): pipx install "madeleine-tracker[ui]"
```

**方式二：源码树（开发用）**

```bash
cd ~/Project/madeleine-portfolio-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # 等价 pip install ".[all,dev]"
```

两种方式数据目录规则一致：环境变量 `TRACKER_HOME` > 源码树仓库根 > 平台用户目录
（Linux `~/.local/share/tracker` / macOS `~/Library/Application Support/tracker` /
Windows `%APPDATA%\tracker`）。pip 安装后首次使用先 `tracker init` 生成配置模板。

### 启动 Streamlit 页面

```bash
streamlit run tracker/ui/app.py   # 源码树
tracker-ui                        # pip 安装后 (等价, 参数透传给 streamlit)
```

页面默认运行在 `http://localhost:8501`，九个页面各有独立 URL（刷新 / 书签 / 前进后退不丢页）。触控板双指横滑不会触发浏览器前进/后退手势（侧边栏持仓/自选表横向滚动到边界时最易误触），返回上一页请用浏览器后退按钮或 `Alt+←`：

- **组合** (`/`) — 持仓编辑、明细表、资产配置（按市场 / 按币种 / 按代码三张饼图，按代码为市值 Top 20 + 其他 + 持仓市值 Top 15 柱图）、净值走势、自选提醒
- **K线** (`/kline`) — 任意代码 K 线查询（支持按代码或名称模糊搜索）+ 多股走势对比 + 加密货币对比（K线页子功能）
- **指数K线** (`/index`) — 宏观/风险指数查询（IX.<KEY> 分组下拉，分组随目录维护；CLI `index-kline --list` 可查全部已收录指数）
- **基本面** (`/fundamentals`) — 顶部切换「估值指标 / AH 溢价」：
  - **估值指标** — 任意 A股/港股/美股代码，最新 PE(TTM)、PB、股息率、5 年平均股息率（OpenBB/yfinance，6h 缓存）；历史 PE/PB 曲线按近 3/6/12/24/36/60 个月展示，并给出区间均值/高低点/分位（百度股市通，6h 缓存）
  - **AH 溢价** — 可选 A+H 股票对（东财目录，失败退回内置常见股票对，也可手动输入代码），按近 3/6/12/24/36/60 个月展示 A 股收盘 ÷ (H 股收盘 × 历史汇率) − 1，0% 为两地同价
- **日历** (`/calendar`) — 持仓/自选的财报、预约披露、除权除息/派息事件月历（窗口与范围可切），可一键导出 `.ics` 导入本地日历
- **导入** (`/import`) — IBKR 账户 / 长桥账户 / 链上钱包 / A股券商文件导入
- **数据健康** (`/health`) — 数据源成功率 / p50·p95 / 最近成功与失败 / 最近错误；组合行情·K线·汇率缓存覆盖与缺口；本地 JSON/缓存库/目录权限体检；缓存来源分布与保温线程状态；可一键主动探测
- **设置** (`/settings`) — 涨跌配色（红涨绿跌/绿涨红跌）、数据源开关与拖动回退顺序（含 akshare / IBKR / 长桥 / EODHD / 加密源）、基础货币

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

# 历史净值: 保存今日快照 / 查看走势 / 按当前持仓回填
python -m tracker.cli snapshot --save            # 快照并落一条净值
python -m tracker.cli nav                        # 最近 180 天净值表
python -m tracker.cli nav --days 30 --json       # 机器可读
python -m tracker.cli nav --backfill --days 365  # 回填一年 (当前持仓 × 历史价)
python -m tracker.cli nav --backfill --dry-run   # 只预览, 不写库

# 查询单个/多个代码实时行情
python -m tracker.cli quote AAPL 600519.SS BTC-USD
python -m tracker.cli quote AAPL --refresh   # 单支强制刷新: 跳过 5min 磁盘缓存与失败负缓存

# 数据健康体检（默认只读本地; 异常退出码 1，可接告警）
python -m tracker.cli doctor                  # 近 24h 来源成功率 + 缓存覆盖 + 完整性
python -m tracker.cli doctor --window 72      # 近 3 天
python -m tracker.cli doctor --json           # 机器可读（含全部来源明细与逐代码缓存年龄）
python -m tracker.cli doctor --probe          # 主动打一次真实行情/汇率（慢）

# 汇率查询
python -m tracker.cli fx USD CNY EUR

# 近 12 个月历史价格
python -m tracker.cli history AAPL --months 12

# 生成 K 线蜡烛图（交互式 HTML）
python -m tracker.cli kline AAPL --months 12
python -m tracker.cli kline 600519.SS --ma 5,10,20,60
python -m tracker.cli kline 0700.HK --period weekly --open

# 宏观/风险指数K线（独立于股票 K 线）
python -m tracker.cli index-kline IX.DXY --months 12      # 美元指数
python -m tracker.cli index-kline IX.VIX --period weekly  # 恐慌指数 周K
python -m tracker.cli index-kline IX.AU --months 24       # 黄金主力连续（商品期货同理: IX.AG/JD/M/A/B/LH/LC）
python -m tracker.cli index-kline IX.URANIUM --months 60  # 铀现货价（Cameco 月K; 长协 IX.URANIUM_LT）
python -m tracker.cli index-kline --list                 # 全部已收录指数（按分组）
python -m tracker.cli index-kline --list --json          # 机器可读目录（code/name/group）

# 代码/名称搜索 → 规范代码（结果可直接喂 quote/kline/compare）
python -m tracker.cli search 腾讯 --json

# 多股走势对比（归一化起点=100）
python -m tracker.cli compare AAPL 0700.HK 600519.SS
python -m tracker.cli compare 601155.SS 1030.HK --ratio 601155.SS 1030.HK  # 比例线
python -m tracker.cli compare AAPL NVDA --refresh  # 强制刷新 (含比例线历史汇率缓存)

# 多股市值对比（统一折算币种; A股东财历史总市值, 美股/港股等历史股本×收盘价）
python -m tracker.cli compare 0700.HK 600519.SS --metric mcap

# 持仓管理
python -m tracker.cli portfolio list
python -m tracker.cli portfolio add AAPL --quantity 10 --avg-cost 180
python -m tracker.cli portfolio remove AAPL
python -m tracker.cli portfolio set-base USD

# 自选股管理
python -m tracker.cli watchlist list
python -m tracker.cli watchlist triggered                    # 只输出到达阈值的自选
python -m tracker.cli watchlist triggered --json             # 机器可读 (triggered 数组)
python -m tracker.cli watchlist add NVDA --list 科技,美股 --upper1 260
python -m tracker.cli watchlist remove NVDA
python -m tracker.cli watchlist import-longport --dry-run  # 长桥自选导入预览（先 longport-login）
python -m tracker.cli watchlist import-longport --overwrite  # 覆盖现有自选

# 导出自选监控报告
python -m tracker.cli report
python -m tracker.cli report -f json -o report.json
python -m tracker.cli report -f csv -w 科技

# 导出组合快照报表（CSV / JSON / Markdown）
python -m tracker.cli export -f md -o snapshot.md
python -m tracker.cli export -f csv -o holdings.csv

# 导出财经日历为 iCalendar（可导入 Apple 日历 / Google Calendar / Outlook）
python -m tracker.cli calendar --ics -o calendar.ics
python -m tracker.cli calendar --months 3 --ics | pbcopy   # macOS 可直接粘贴导入
# 同一事件 UID 稳定: 重复导出/导入会更新原事件; 财报同季度内改期也不会新增重复条目

# 从链上地址查询加密资产余额（只读，支持 eth/bsc/polygon/arbitrum/avalanche/tron/solana）
python -m tracker.cli import wallet eth 0xd8dA...6045 --dry-run  # 仅查询
python -m tracker.cli import wallet tron TQxxxx...             # TRON 链 (base58 地址) 追加写入
python -m tracker.cli import wallet solana Hxxxx...            # Solana 链 (base58 公钥)

# 行情磁盘缓存管理
python -m tracker.cli cache info
python -m tracker.cli cache clear

# 主动预热 / 常驻保温 (不依赖 Streamlit 前端进程; 与页面共用同一磁盘缓存)
python -m tracker.cli warm                          # 预热一次 (持仓+自选)
python -m tracker.cli warm --symbols AAPL TSLA      # 额外代码也预热
python -m tracker.cli warm --calendar-only --json   # 只预热财经日历
python -m tracker.cli warm --interval 300           # 常驻, 每 5 分钟检查一次

# 统一导入（追加合并为默认，--overwrite 覆盖全部持仓）
python -m tracker.cli import ibkr --dry-run            # 预览 IBKR 账户持仓（需 Gateway 已登录）
python -m tracker.cli import ibkr --mode live --overwrite
python -m tracker.cli import wallet eth 0xd8dA...      # 链上钱包余额追加导入
python -m tracker.cli import file 持仓.csv --dry-run   # 券商导出 CSV/Excel 解析预览
python -m tracker.cli import longport --dry-run        # 长桥账户持仓预览（先 longport-login 登录）
# 兼容入口: sync / import-wallet / python -m tracker.ibkr_sync|ashare_sync 仍可用

# 长桥 (LongPort) 行情与登录（可选源，失败自动回退默认数据源）
python -m tracker.cli longport-login                       # OAuth 浏览器授权登录（token 自动缓存刷新）
python -m tracker.cli quote AAPL 0700.HK --longport --json # 指定子命令走长桥行情

# EODHD (BYOK: 自备 token, 可选付费源; 未配置时不影响免费链路)
python -m tracker.cli eodhd status                         # 查看是否可用 / token 掩码 / 角色
python -m tracker.cli eodhd test AAPL 0700.HK --json       # 实打一次行情 + 最近 7 天历史 + 基本面

# 运行单元测试（全离线；禁网护栏兜底：漏打桩的测试秒红而非挂起）
python -m pytest tests/ -q

# 真实联网集成测试（仅 @pytest.mark.network 标记的用例，默认不运行）
python -m pytest -m network

# 线上数据契约（各域真实行情/历史/指数/汇率；源改字段时第一时间红）
python -m pytest -m network tests/test_provider_contract_live.py -q
```

> 完整 CLI 帮助：`python -m tracker.cli --help`（或每个子命令 `--help`）。`python -m tracker` 等价。
> pip 安装后可直接用 `tracker` 命令（`tracker --help`）；`tracker init` 首次生成配置模板。

---

## 供 Agent 接入的 CLI 能力

所有子命令均支持 `--json` 输出机器可读结果，专为 hermes / openclaw 之类手机 Agent 设计：

```bash
python -m tracker.cli snapshot --json          # 持仓 + 自选 + 阈值触发，一次拿全
python -m tracker.cli quote 0700.HK --json     # 单/多代码实时行情
python -m tracker.cli watchlist list --json    # 自选 + 行情 + 触发状态
python -m tracker.cli watchlist triggered --json  # 只含到达阈值的条目 (triggered/count)
python -m tracker.cli kline AAPL --json        # K 线数据（不生成图表）
python -m tracker.cli index-kline IX.DXY --json # 指数K线数据 (IX.<KEY>, 不生成图表)
python -m tracker.cli export -f json           # 完整快照 JSON
python -m tracker.cli longport-login --json    # 长桥 OAuth 授权 URL（脚本消费，不自动开浏览器）
python -m tracker.cli eodhd status --json      # EODHD BYOK 是否可用（token 掩码，不泄露明文）
python -m tracker.cli eodhd test AAPL --json   # EODHD 连通性自检（行情 + 最近 7 天历史 + 基本面）
python -m tracker.cli warm --json              # 预热缓存（可 --interval 常驻，供后续命令秒回）
```

- 无交互、无 TUI 依赖，纯 stdout；错误走 stderr 并以非零码退出
- 汇总/明细一次请求返回，适合小上下文 Agent 直接消费

---

## 代码规范（自有后缀规范 + 内部 type 字段）

后缀规范是**本项目自定义**的（恰好与 Yahoo Finance 兼容）；域的权威判据是系统内部维护的 **`type` 字段**
（`global`/`cn`/`crypto`，指数域为 `index`），在 portfolio/watchlist 保存与 IBKR/A股/钱包导入时自动推导覆写，用户不可见。

| 市场 | 后缀 | 示例 | 币种 | type | 说明 |
|------|------|------|------|------|------|
| 美股 | 无 | `AAPL` | USD | global | |
| A股 | `.SS` / `.SZ` | `600519.SS` `000001.SZ` | CNY | cn | 也接受 `.SH` |
| B股 | `.SS` / `.SZ` | `900902.SS` `200012.SZ` | USD / HKD | cn | 上海 B 股 `9` 开头以**美元**交易，深圳 B 股 `2` 开头以**港币**交易 |
| 北交所 | `.BJ` | `830799.BJ` | CNY | cn | |
| 港股 | `.HK` | `0700.HK` `82318.HK` `9804.HK` | HKD / CNY / USD | global | **4 位补零**（不足 4 位补前导零，`00700.HK` 自动归一）；多柜台模型下同一标的有独立代码：人民币柜台为 `8` 开头 5 位码（如 `82318.HK` 中国平安-R，以 **CNY** 计价）、美元柜台为 `09xxx`/`41xxx`（如 `9804.HK` PP越南-U、`09070.HK` 平安香港高息-U、`09188.HK` 华夏沪深三百-U，以 **USD** 计价），原样支持、不截断。美元柜台只属于 ETF 的港元/人民币/美元三柜台，股票的港币-人民币双柜台没有美元柜台；其币种由数据源自报（长桥 `static_info`/IBKR 合约/yfinance），**不能按代码形态推导** —— `normalize` 剥前导零后 `09070.HK`→`9070.HK` 与 `9988.HK` 阿里同形 |
| 德股 | `.DE` 等 | `SAP.DE` | EUR | global | `.F` `.BE` `.DU` `.HM` `.MU` 均可 |
| 英股 | `.L` | `BP.L` | GBP | global | Yahoo 报价单位为便士（GBp），系统自动 ÷100 换算为英镑；`.AL` 同属伦交所；`.IL` = LSE 国际挂盘 IOB（如 `KAP.IL` Kazatomprom GDR，**USD 计价**，不做便士折算） |
| 加股 | `.TO` 等 | `RY.TO` | CAD | global | `.V`（TSXV）`.CN` `.NE` 均可 |
| 澳股 | `.AX` | `BHP.AX` | AUD | global | |
| 日股 | `.T` | `9984.T` `6758.T` | JPY | global | 东京证券交易所（JPX）；`.JP` 别名自动归一为 `.T`；代码为 4 位数字，不补零 |
| 韩股 | `.KS` / `.KQ` | `005930.KS` `005935.KS` `035720.KQ` | KRW | global | KRX 韩国交易所：`.KS`=KOSPI 主板、`.KQ`=KOSDAQ；6 位代码保留前导零；普通股与优先股为并列代码（三星电子 `005930.KS` / 优先股 `005935.KS`）；`.KR` 不作为别名（两板同族无法判归哪板） |
| 新加坡股 | `.SI` | `D05.SI` `NIO.SI` | SGD / USD | global | SGX；`.SG` 别名自动归一为 `.SI`（yfinance 无 `.SG` 数据）；SGX 同时有 SGD 计价（`D05.SI`）与 USD 计价（`NIO.SI`）证券，IBKR 侧按代码自辨币种 |
| 巴西股 | `.SA` | `PETR4.SA` | BRL | global | B3；IBKR 交易所代号 `B3`（旧 `BVMF` 已下线） |
| 加密货币 | `-QUOTE` | `BTC-USD` `ETH-USDT` | 计价货币 | crypto | 连字符 `BASE-QUOTE`（Yahoo crypto 规范）；无分隔符 `BTCUSD` 自动补全；稳定币计价（USDT/USDC/FDUSD…）按 USD 等价估值 |

**互斥保证**：点后缀/裸代码永远是股票，连字符+计价货币永远是加密货币（单字母连字符 `BRK-B`
是美股类别股、`P+系列字母` 连字符如 `WFC-PL` 是美股优先股，都不是 crypto）。推导唯一入口 `symbols.type_for_symbol()`；测试用例、搜索结果、
IBKR/A股/钱包导入产出的代码均符合该规范。

`avg_cost`（成本）按**当地货币**填写；英股填**英镑**（如 `4.30 = £4.30`，不是便士）；
B 股按**实际交易币种**填写（沪 B 美元 / 深 B 港币，与行情币种一致）。

---

## K 线查询（按代码或名称）

K线页支持两种输入方式：

- **代码直查** — `AAPL` / `600519.SS` / `0700.HK` / `SAP.DE` / `BP.L` / `BTC-USD`
- **名称模糊搜索（分域）** — 输入代码或名称片段（如「茅台」「Tencent」「BTC」「bitcoin」），每个 provider 提供自己的搜索接口，UI 按「A股/北交所 · 美股/港股 · 加密货币」三块下拉分组展示，互不混排；美股/港股走 IBKR 合约匹配（Gateway 在线时优先）→ 东财 suggest（中文名/纯数字补零，只认领港股行，故「腾讯」「700」可命中 0700.HK）→ yfinance Search，A股/北交所走东财 suggest（中文名/拼音），加密货币走 yfinance Search（返回即 Yahoo 规范代码 BTC-USD）；纯在线搜索接口封装 + 归一化，无本地目录、无缓存快照，接口失败即空结果 + 合法代码直查
- **快捷 pills** — 「常用 (持仓/自选)」只展示前 10 个（持仓在前），持仓/自选很多时页面保持整洁；「最近查看」记录本会话最近成功查询的 10 个代码（最新在前，点击即回填查询，与常用行去重不重复）

页面端基于 TradingView 开源的 **lightweight-charts** 渲染（已 vendored，无新增依赖），券商 App 通用交互：

- **蜡烛图 + 成交量副图 + MA 均线**（默认 MA5/20/60，可自定义），默认红涨绿跌（设置页可切国际配色）
- **技术指标** — MACD / RSI / KDJ / 布林带，多选叠加，周期参数可调
- **拖动 = 平移**（带惯性滚动），**滚轮/捏合 = 缩放时间轴**，触控板双指横滑平移、双指纵向/捏合缩放（Chrome/Edge/Firefox 的 `ctrl+wheel` 与 Safari 的 gesture 事件均已适配，捏合不会触发浏览器整页缩放）
- **右键菜单** — 图表上右键可重置缩放、复制代码、复制数据 (CSV)、导出 PNG；表格上右键可复制单元格/整行/含表头，并可直接「查看 K 线」；持仓/自选表还提供「从持仓/自选删除」（复用确认弹窗，不绕过二次确认）
- **滑动模式一次加载上市以来全量历史** — 图表内拖动/缩放全程纯前端，不触发取数；另有「范围」模式按月窗口兜底（3/6/12/24/36/60 个月，最长 5 年）
- 十字光标 + 顶部 OHLC/涨跌/量/均线信息栏；价格轴随可见区间自动缩放；副图分隔线可拖拽；双击轴复位
- **非交易日断轴**：周末/节假日不出空隙，图形连续
- **日K / 周K / 月K** 一键切换（周K 按 W-FRI 对齐）
- 数据经过**清洗校验**（去重/排序，丢弃 high<low 等自相矛盾的脏行），英股便士自动换算
- **双层缓存**：SQLite 磁盘缓存 30 分钟（`cache info` 可查看），页面另有内存缓存；CLI `--refresh` 强制刷新
- **强制刷新**：页面「强制刷新」按钮 / CLI `--refresh`（`quote`/`kline`/`compare`/`calendar`）会清对应页面缓存、跳过对应磁盘缓存（行情/K线/日历/市值），并穿透基本面/汇率历史等负缓存；基本面页的估值快照/历史 PE/PB 与 AH 溢价也有独立刷新按钮；单只查询页只失效当前代码的页面缓存，不影响其它代码。数据源偶发失败后可立即重试，不必等缓存或 6h 负缓存过期

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
- **可选比例线**（默认关闭）：打开后选两个对比代码（分子/分母），在同一张图上叠加
  两标的收盘价的**具体比值**（不归一化），使用独立纵轴，虚线为区间均值，
  悬停显示当日比值与较均值的偏离幅度。
  跨币种默认直接看两个**原币报价**的比值；也可打开「汇率调整」按历史汇率统一口径
  （如 HKD→CNY），避免比值被汇率波动带偏。命令行对应 `--ratio 分子 分母`
  （`--ratio-fx` 打开汇率调整）。

「K线」页还有第三个子功能「**加密货币对比**」（仅 UI 提供，无对应 CLI 子命令）：只接受
`BASE-QUOTE` 加密货币代码，各币画**独立窗格蜡烛图**（BTC ~10 万 vs SHIB ~0.00002 量级悬殊，
同轴归一化不可读，故不做归一化、保留原始价格），共享时间轴与十字光标，下方同样给出区间涨跌。

> 市值指标（`--metric mcap` / 页面「指标: 市值」）的序列币种跟随实际报价币种：
> `.IL`、港股人民币/美元柜台等按数据源自报换算，统一折算前不会按市场默认币种硬编码。

> 比值线（两标的收盘价相除画成折线）已支持，见上；把比值画成 K 线（OHLC）仍在 Roadmap 中。

---

## 历史净值

组合页 KPI 卡片下方的「净值走势」读取每日净值历史，画**总市值**与**成本**两条线，
可切 30 / 90 / 180 / 365 天或全部；没有历史时给出保存/回填提示。

- **存储**：独立 SQLite `<数据目录>/nav_history.db`（`TRACKER_NAV_DB` 可覆盖），
  与行情缓存库分离 —— `tracker cache clear` 或缓存库损坏隔离不会删掉历史。
  主键 `(日期, 基础货币)`，同一天重复保存覆盖。
- **自动存档**：组合页每次成功渲染都会 upsert 当天记录（同日覆盖、随后台刷新更新，
  冷启动行情不全时的偏低值会被后续渲染修正）；CLI 用 `snapshot --save` 或 `nav --save` 显式存档。
- **历史回填**：`nav --backfill --days 365` 用**当前持仓数量 × 历史收盘价**
  （跨币种按历史汇率折算）生成近似曲线，只覆盖没有真实快照的日期
  （真实快照优先）；重新回填会更新旧的回填行。该口径不含历史调仓，
  不能当作真实历史业绩；`--dry-run` 可先预览。
- **JSON**：`nav --json` 输出 `{base, count, rows[]}`；回填输出
  `{base, action, days, generated, inserted, dry_run, issues[]}`。

### 钱包净值曲线（链上转账回放）

单个链上地址的历史净值，独立于 `nav_history.db`，按需查询、不写组合：

```bash
python -m tracker.cli wallet-nav eth 0xd8dA...6045 --days 90
python -m tracker.cli wallet-nav eth 0xd8dA...6045 --days 365 --json
python -m tracker.cli wallet-nav tron T... --base CNY --crypto-source binance
python -m tracker.cli wallet-nav solana H... --max-assets 20
```

- **口径**：先取当前链上余额，再用窗口内的转账/交易/手续费从今天往前反推
  每个 UTC 自然日的收盘余额，最后按 `SYMBOL-USD` 历史日线与 `USD→基础货币`
  历史汇率折算。**不是**「当前余额 × 历史价」，因此能反映历史转入/转出/交易。
- **来源**：Ethereum 走 Routescan（Blockscout 兜底）；Avalanche 走 Snowtrace /
  Routescan；TRON 走 TronGrid；Solana 走公共 RPC（`getSignaturesForAddress` +
  `getTransaction`）。以上零 API Key。Polygon / Arbitrum / BSC 的免费公共浏览器
  接口不稳定或需要 key：可在 `wallet_history.json` 配置 `explorer_key`
  （Etherscan V2，或环境变量 `TRACKER_EXPLORER_KEY`），或用 `explorers` /
  `blockscout_urls` 指向自建/其它兼容服务（`tracker init` 会生成模板）。
- **限制**：质押/解冻中的 TRX 不计入；rebasing / fee-on-transfer 代币、浏览器索引
  缺失、以及 Solana 公共节点裁剪历史可能造成偏差；代币垃圾空投会被忽略，资产
  数量超过 `--max-assets`（默认 30）时按当前估值优先保留大头，跳过项在 `notes`
  中列出。曲线用于看趋势，不作为审计口径。
- **JSON**：输出 `{chain, address, base, start, end, rows[], assets[], issues[], notes[]}`。

---

## 自选股与阈值提醒 (价格 / 市净率 / 市值)

`watchlist.json` 为扁平条目列表，**一个代码可属于多个列表**（`lists` 数组，逗号分隔多归属），支持**两级阈值**（`upper_1`/`upper_2`、`lower_1`/`lower_2`）、**指标基准**（`metric`：`price`=现价，默认；`pb`=市净率；`mcap`=总市值）与备注。旧格式（扁平 `upper`/`lower`、嵌套 `{"watchlists": {...}}`）自动迁移：

```json
{
  "watchlist": [
    { "symbol": "TSLA", "lists": ["科技", "美股"], "upper_1": 420, "upper_2": 450, "lower_1": 280, "lower_2": 250, "note": "两级提醒" },
    { "symbol": "600036.SS", "lists": ["银行", "A股"], "metric": "pb", "upper_1": 1.2, "lower_1": 0.8, "note": "PB提醒" },
    { "symbol": "AAPL", "lists": ["美股"], "metric": "mcap", "upper_1": 4000000000000, "lower_1": 3000000000000, "note": "市值提醒" },
    { "symbol": "BRK-B", "lists": ["默认"], "note": "无阈值纯观察" }
  ]
}
```

- **同一代码属于多个列表**：如 TSLA 同时在「科技」和「美股」，配置集中在一处，改一次全生效
- **可选 `name` 显示名**：日历页在数据源没有名称时用 `name` 补全事件名称；页面保存自选会保留编辑器未展示的字段（含 `name` 与自定义键），不会丢配置
- **阈值基准 `metric`**：`price`（默认，可省略）= 按现价判定，阈值按当地货币；`pb` = 按市净率（现价 ÷ 每股净资产）判定，阈值是 PB 倍数；`mcap` = 按总市值判定，阈值是当地货币绝对金额。指标基准以 `metric` 为准：显式写 `metric`（`price`/`pb`/`mcap`）时以它为准；**省略 `metric` 时**，若条目含手写 `pb`（每股净资产）/ `mcap`（总市值）覆盖键，则基准推断为对应指标，否则按 `price`——即手写键既是数据覆盖值，也会在缺省 `metric` 时暗示基准。页面「指标基准」留空会显式钉为 `price`，不受此推断影响；若想保留 `pb`/`mcap` 覆盖键又用 `price` 基准，需显式写 `metric: "price"`。每股净资产来源按优先级：条目手写 `pb` 键 (离线可用) > 行情自带 (长桥 calc_indexes / akshare 东财, 实时口径) > Yahoo Finance fundamentals (缓存 6 小时)；总市值来源同优先级：条目手写 `mcap` 键 > 行情自带 (akshare 东财 / 长桥) > fundamentals。crypto 与无数据标的指标缺失时显示「指标无数据」，不误判触发
- 触发等级（从高到低）：`🔴 突破上限 II` / `🟠 突破上限 I` / `🟢 跌破下限 II` / `🟡 跌破下限 I`；触发项排在最前
- 距离列：`距上限 I%` / `距上限 II%` / `距下限 I%` / `距下限 II%` = 触发还需变动百分之几（上下限同口径；上限为负 / 下限为正 = 已越过）
- 页面侧栏「所属列表」列用逗号分隔编辑多归属；「自选观察」tab 顶部「查看范围」选 `全部` 或单个列表
- CLI：`--watchlist <列表名>` 只看某个列表（`全部` 合并去重）
- CLI 只看触发的标的：`tracker watchlist triggered`（`--json` 输出 `triggered` 数组与 `count`，适合脚本/Agent 消费）
- CLI 添加 PB 提醒：`tracker watchlist add 600036.SS --metric pb --upper1 1.2 --lower1 0.8`
- CLI 添加市值提醒：`tracker watchlist add AAPL --metric mcap --upper1 4000000000000`
- 持仓与自选**共用一次批量行情请求**，多加自选不增加请求次数

---

## 架构

```
tracker/ui/           Streamlit 页面包（streamlit run tracker/ui/app.py / 安装后 tracker-ui）
├── app.py              路由入口（st.navigation 多页面, 每页独立 URL, 刷新不丢页）
│                          + cli_main(): tracker-ui 控制台入口（定位包内 app.py 转交 streamlit）
├── portfolio_page.py   「组合」页面（持仓编辑/明细/配置/自选提醒/净值走势 + 行情/汇率缓存）
├── kline_page.py       「K线」页面（搜索框 + lightweight-charts 组件 + 上市以来全量滑动 + 多股走势对比 + 加密货币对比）
├── index_page.py       「指数K线」页面（IX.<KEY> 分组下拉, 独立于股票 K线页）
├── fundamentals_page.py 「基本面」页面（估值指标 PE/PB/股息率 + 历史 PE/PB 分位; AH 溢价曲线: A+H 股票对 / 范围切换, 明细表）
├── import_page.py      「导入」页面（IBKR 账户 / 长桥账户 / 链上钱包 / 券商文件, 追加合并或覆盖）
├── wallet_page.py      「钱包净值」页面（链上转账回放历史余额 + 历史币价/汇率折算曲线）
├── health_page.py      「数据健康」页面（来源成功率/耗时/最近失败 + 缓存覆盖/缺口 + 完整性 + 主动探测）
├── settings_page.py    「设置」页面（配色 / 数据源 / EODHD BYOK / 基础货币）
├── longport_login.py   长桥 OAuth 登录引导（独立子进程, 状态文件 <数据目录>/var/longport_oauth_state.json）
└── settings.py         settings.json 读写（涨跌配色 / prefer_akshare / use_ibkr / use_longport / crypto_source / source_order）+ portfolio 读写助手
tracker/
├── __main__.py         python -m tracker 入口（转发到 cli）
├── paths.py            数据目录解析（TRACKER_HOME > 仓库根 > 平台用户目录）+ 包内资源定位
├── health.py           数据健康观测（回源事件记录/聚合、缓存覆盖、完整性体检、主动探测）
├── cli/                CLI 包（python -m tracker.cli，24 个子命令各一个模块）
│   ├── __init__.py       argparse 注册与分发
│   ├── _common.py        JSON 序列化 / 代码归一化 / 原子写 / 错误退出
│   ├── __main__.py       python -m tracker.cli 入口
│   ├── init.py           首次使用: 包内模板 → 数据目录（pip 安装后必走一步）
│   ├── doctor.py         数据健康体检（来源成功率/缓存覆盖/完整性, --probe 联网）
│   ├── eodhd.py          EODHD BYOK 状态 / 连通性自检
│   ├── snapshot.py       快照（持仓 + 自选 + 阈值触发; --save 落历史净值）
│   ├── nav.py            历史净值（读取 / 保存今日快照 / 按当前持仓回填）
│   ├── wallet_nav.py     钱包历史净值（链上转账回放 + 历史币价/汇率折算）
│   ├── quote.py          实时行情
│   ├── watchlist.py      自选管理（list/add/remove/triggered/import-longport）与阈值提醒
│   ├── portfolio.py      持仓管理（list/add/remove/set-base）
│   ├── report.py         自选监控报告（md/csv/json）
│   ├── export.py         组合快照导出（csv/json/md）
│   ├── fx.py             汇率
│   ├── history.py        历史价格
│   ├── kline.py          K线 HTML + 摘要（--json 输出数据）
│   ├── compare.py        多股走势/市值对比（归一化折线, --raw 原价, --ratio 比例线, --metric mcap 市值, --json 出序列）
│   ├── search.py         代码/名称搜索 → 规范代码
│   ├── index_kline.py    指数K线 HTML + 摘要（IX.<KEY>, --json / --list）
│   ├── importer.py       统一导入（import ibkr / longport / wallet / file, 追加/覆盖）
│   ├── sync.py           IBKR 持仓同步（兼容入口 → import ibkr, 缺省覆盖全部持仓）
│   ├── wallet.py         链上钱包导入（兼容入口 → import wallet, 缺省只查询不写盘）
│   ├── longport_login.py 长桥 OAuth 登录（浏览器授权, token 自动缓存与刷新）
│   ├── cache.py          磁盘缓存管理（info / clear）
│   └── warm.py           缓存保温/预热 CLI（一次预热 / --interval 常驻）
├── storage.py          所有 JSON 配置唯一读写口（portfolio/watchlist/settings, 原子写 + 损坏时尝试历史 .bak 恢复）
├── nav.py              历史净值存储（独立 SQLite nav_history.db）+ 按当前持仓回填
├── symbols.py          自有代码规范、市场域识别、type_for_symbol() 权威域推导（唯一入口）
├── symbol_migrations.py 退市/改码 → 接替代码迁移表（长桥导入时自动迁移）
├── util.py             with_timeout 硬超时等工具
├── providers/          **数据 provider 层**（按权威 type 域隔离）
│   ├── base.py         Provider 基类 + Quote/SymbolEntry 数据结构 + resolve(type) 域路由
│   ├── cn_stocks.py        A股/B股域（.SS/.SZ/.BJ）：akshare 固定优先, yfinance 兜底
│   ├── global_stocks.py    全球股票域（美股/港股/欧股…）：yfinance 主源, 港股 akshare 兜底; 搜索走 IBKR → 东财 suggest → yfinance Search → 代码直查
│   ├── crypto.py           加密货币域（BASE-QUOTE）：Binance → Gate.io → Hyperliquid(USD系) → yfinance
│   ├── index.py            指数域（IX.<KEY>）：中国指数 akshare 优先, 其余 yfinance 主源 + 新浪兜底; 水泥网/商品期货/恒生官网/Cameco 铀价为专属源
│   ├── orchestration.py    批量编排：按域分组取数 → 聚合 quotes/errors/notes; 进程内负缓存
│   └── em_suggest.py       东财 suggest 搜索客户端（cn 域搜索底层 HTTP 封装 + 代码归一）
├── prices.py           行情门面（历史 API 保持不变, 全部路由到 providers）
├── search.py           搜索聚合层：search_grouped() 按域调 provider.search() 保持分组；纯在线接口，失败即该域空结果 + 合法代码直查兜底
├── fx.py               汇率：CFETS（akshare）优先，yfinance 货币对兜底（直对/逆对/USD 桥）
├── cache.py            行情/基本面 SQLite 磁盘缓存（实时 5 分钟 / K线 30 分钟 / 基本面 6 小时）
├── charting.py         K线渲染（plotly CLI HTML + lightweight-charts 页面组件：蜡烛/均线/指标/对比/单序列参考线）
├── ah_premium.py       AH 溢价率（A+H 股票对目录 / A/H+历史汇率对齐 / 溢价曲线与摘要, 基本面页 AH 子视图）
├── valuation.py        估值指标（PE/PB/股息率快照 + A股/港股/美股历史 PE/PB 曲线, 基本面页估值子视图）
├── analytics.py        组合视图与指标（纯函数）
├── watchlist.py        自选配置存储门面（读写委托 storage，仅 I/O 归一）
├── services/           **独立用例层**（用例 = 取数 providers/fx + 纯函数计算 + 组装视图）
│   ├── rules.py          阈值规则（纯函数：两级上下限状态判定 + 距离）
│   ├── watchlist.py      自选视图组装 + 取数用例（watchlist list / report 复用）
│   ├── snapshot.py       快照用例（take_snapshot / snapshot_json, 存储剥离）
│   └── warm.py           缓存保温/预热服务（无 Streamlit 依赖；UI 与 CLI `warm` 共用）
├── fundamentals.py     基本面取数门面（每股净资产/总市值, 6h 磁盘缓存）— PB/市值提醒数据源
├── mcap.py             历史市值序列（A股东财历史总市值 / 全球历史股本×收盘价 / 当前市值近似; 市值对比用）
├── ibkr.py             IBKR 行情接入 + 持仓读取（可选依赖 ib_async，失败静默回退）
├── longport.py         长桥行情/历史/持仓接入（可选依赖 longport，失败静默回退）
├── eodhd.py            EODHD BYOK 接入（token 自备; 行情/历史源, 配置与自检, 失败静默回退）
├── longport_oauth.py   长桥 OAuth 授权子进程（独立进程运行, 状态写 var/longport_oauth_state.json）
├── importer.py         **统一导入管道**：四来源采集 → 追加/覆盖合并 → 原子写盘
├── ibkr_sync.py        IBKR 持仓导入兼容入口（委托 importer，等价 import ibkr，缺省追加合并）
├── ashare_sync.py      A股券商文件解析 + 兼容入口（等价 import file，缺省追加合并）
├── wallet.py           链上钱包余额查询（EVM 五链 + TRON + Solana, 只读）
├── wallet_history.py   链上钱包历史余额回放（Routescan / Blockscout / Etherscan / Snowtrace / TronGrid / Solana RPC）
└── wallet_nav.py       钱包净值曲线（历史余额 × 历史币价 × 历史汇率）
数据目录（$TRACKER_HOME > 源码树仓库根 > 平台用户目录, 规则见 tracker/paths.py;
pip 安装后用户数据不再写进 site-packages, 升级/重装不丢）:
portfolio.json          持仓配置（页面可直接编辑保存; type 字段由系统自动维护, 手改无效;
                         用户本地数据, 已 gitignore, 模板 tracker/templates/portfolio.example.json）
watchlist.json          自选股配置（页面可直接编辑保存; type 字段由系统自动维护, 手改无效;
                         用户本地数据, 已 gitignore, 模板 tracker/templates/watchlist.example.json）
settings.json           显示与数据源设置（配色 / use_ibkr / use_longport / source_order;
                         旧 prefer_akshare / crypto_source 仍兼容; 已 gitignore,
                         模板 tracker/templates/settings.example.json）
ibkr.json               IBKR 真实配置（已 gitignore; 模板 tracker/templates/ibkr.example.json,
                         env IBKR_CONFIG 可指定其他路径）
longport.json           长桥真实配置（已 gitignore; 模板 tracker/templates/longport.example.json,
                         env LONGPORT_CONFIG 可指定其他路径）
eodhd.json               EODHD 真实配置（BYOK token / 角色 / 复权口径; 已 gitignore, 模板
                         tracker/templates/eodhd.example.json, env EODHD_CONFIG 可指定其他路径;
                         token 也可用 env TRACKER_EODHD_TOKEN 注入, 优先级更高）
wallet_history.json      链上钱包历史查询配置（可选 Etherscan V2 key / 自定义浏览器; 已 gitignore,
                         模板 tracker/templates/wallet_history.example.json, env
                         TRACKER_WALLET_HISTORY_CONFIG 可指定其他路径; key 也可用
                         env TRACKER_EXPLORER_KEY 注入）
var/                    运行时生成产物（行情缓存 quotes_cache.db、kline/compare 图表 HTML、
                         longport_oauth_state.json 长桥登录状态）, 位于数据目录下,
                         可随时整目录删除（重建即自动再生成）
tracker/templates/      包内配置模板 + streamlit 主题（随 wheel 分发; tracker init 的来源）
tracker/assets/         vendored lightweight-charts standalone JS（离线内嵌, 随 wheel 分发）
tracker/ui/components/  本地自定义 Streamlit 组件（数据源顺序拖动编辑器, 零新增前端依赖）
pyproject.toml           打包元数据（依赖 extras / console scripts / package-data）
Dockerfile / docker-compose.yml   UI 容器镜像（数据落 /data 卷, 一键起）
docs/                   研究笔记（A股券商导入方案调研 / IBKR 只读 API 笔记，后者为改 IBKR 前必读）
skills/                 随仓库分发的 agent 技能手册（portfolio-tracker/SKILL.md: CLI 速查/代码规范/数据源/配置/操作流程）
tests/                  单元测试（离线, mock）
```

首次使用（pip 安装后）：`tracker init` 把上述模板复制到数据目录，再按需修改。

---

## 数据源与降级策略 (provider 层)

数据层按市场域拆分为四个 provider（`tracker/providers/`），只在组合/watchlist 聚合。
路由依据是系统内部维护的权威 `type` 字段（用户不可见），后缀规范仅用于推导 type：

| type | 覆盖代码 | 实时行情优先级 | 历史K线优先级 |
|----------|----------|----------------|----------------|
| **global** | 裸代码（美股）、`.HK/.DE/.L/.TO/.AX/.T/.KS/.KQ/.SI/.SA…` | IBKR(可选, 优先) → 长桥(可选) → **EODHD(可选, primary 时)** → yfinance 批量 → 港股 akshare → EODHD(可选, fallback) | IBKR(可选) → 长桥(可选) → **EODHD(可选, primary)** → yfinance（港股可 `--akshare` 翻转） → EODHD(可选, fallback) |
| **cn** | `.SS/.SZ/.BJ`（A/B股、北交所） | 长桥(可选前置) → **akshare 优先** → yfinance | 长桥(可选前置) → **akshare 优先** → yfinance |
| **crypto** | `BASE-QUOTE`（`BTC-USD`） | **Binance API** → Gate.io 现货 → Hyperliquid 永续（仅USD系） → yfinance | **Binance klines** → Gate.io → Hyperliquid → yfinance |
| **index** | `IX.<KEY>`（`IX.DXY` 美元指数 / `IX.VIX` 恐慌指数 / `IX.CSI300` 沪深 300 / `IX.AU` 黄金 · `IX.AG` 白银 · `IX.JD` 鸡蛋 · `IX.M` 豆粕 · `IX.A` 豆一 · `IX.B` 豆二 · `IX.LH` 生猪 · `IX.LC` 碳酸锂主力连续 / `IX.URANIUM` 铀现货价 · `IX.URANIUM_LT` 铀长协价 / `IX.CEMPI` 水泥价格指数 / `IX.HSI` 恒生指数 · `IX.HSTECH` 恒生科技指数 等恒生官网港股指数 / `IX.HSAHP` AH股溢价指数 …） | —（指数无实时行情, 不参与持仓聚合） | 中国指数 **akshare 优先** → yfinance；美股 SPX/NDX/DJI 有新浪兜底；水泥网/商品期货/恒生官网/Cameco 铀价为专属源（无兜底） |

- **回退顺序可视化编辑**：设置页「数据源 → 回退顺序」按 provider 分别拖动调整
  **实时行情** / **历史K线** 的逐代码源链（自动保存，也支持 ↑/↓ 与 Alt/Ctrl+↑/↓），
  配置写在 `settings.json` 的 `source_order`。顺序从上到下即优先级，手写配置里未列出的源自动补到末尾；
  手改 JSON 同样生效，空对象 `{}` = 沿用上表默认链。
  例：`{"global": {"quote": ["akshare", "yfinance", "eodhd"]}}`。
  IBKR / 长桥也在这份列表里参与排序：`use_ibkr` / `use_longport` 开关只控制是否启用，
  拖到前面就优先命中，拖到 yfinance 后面则只为前面未命中的代码兜底。
- **type 即域**：`type` 字段由系统在保存/导入时按 `symbols.type_for_symbol()` 自动推导覆写，
  手改 JSON 无效。点后缀/裸代码永远是股票，连字符+计价货币永远是加密货币，同一代码不会映射两个域。
  测试用例、搜索结果、导入产出的代码均符合该规范。
- **长桥与 IBKR 同开时**：默认顺序为 IBKR → 长桥（IBKR 命中的代码优先，长桥补未命中项）；历史同理。可在设置页把长桥拖到 IBKR 前面改变优先级
- **IBKR 历史K线按 ≤365 天窗口分块拼接**（IBKR 单次日K请求上限 365 D，超限拒单）：60 个月（5 年）区间在 IBKR 源同样可用；标的上市晚于区间起点时连续空窗即停，任一分块失败该代码整体回退默认源（不返回半截区间）
- 汇率：CFETS 一次拿全 XXX/CNY；直对/逆对/USD 桥接兜底
- A股/港股实时行情（akshare 源）走东财 push2 **单代码**接口（`.SS`=市场段 1、`.SZ`/`.BJ`=0、`.HK`=116+5位码），一个请求拿齐 最新价/昨收/涨跌幅/名称/总市值/市净率，实测 0.4-0.8s；不用 akshare 全市场 spot（内部分 60 页拉全 A 股、页间 sleep，单次 42-102s，秒级超时必然失败——改之前「A股 akshare 优先 / 港股 akshare 兜底」实际从未生效）。单代码接口还比全市场表多给港股的总市值/市净率（全市场 spot 最终列丢了这两列），港股 PB/市值阈值提醒因此有了东财实时口径；带 5s 进程内缓存 + per-key 单飞 + 失败 15s 占位
- `--akshare` 开关仅影响港股数据源顺序（A股域默认 akshare 优先，也可在设置页拖动）；设置页已改为直接拖动排序，
  旧 `"prefer_akshare": true` 仍作为未配置 `source_order` 时的默认链兼容偏好
- 加密货币域默认链 Binance → Gate.io → Hyperliquid(USD系) → yfinance；可直接在设置页拖动排序，
  也可用 `settings.json` `"crypto_source": "auto|binance|gate|hyperliquid|yfinance"`
  或子命令 `--crypto-source <值>` 作为临时前置偏好（Hyperliquid 为永续合约价，与现货有基差，仅 USD 系计价代码）
- IBKR 行情可选叠加于任何域（需订阅）；英股 GBp 便士报价自动换算为 GBP；汇率缓存 10 分钟，行情缓存 5 分钟
- IBKR 连接参数**从配置文件读取**（不写死在代码里）：优先数据目录下的 `ibkr.json`，其次包内模板 `tracker/templates/ibkr.example.json` 兜底（`tracker init` 生成后修改），也可用环境变量 `IBKR_CONFIG=/path/to/xxx.json` 指定；交易所映射在同一文件（A股经沪深港通通道：沪 `SEHKNTL`、深 `SEHKSZSE`、科创 `SEHKSTAR`，计价 CNH；B股走 `SHSE`/`SZSE` 并配 `tradingClass`；模板 `exchanges` 键名与代码读取一致，删键即用内置默认路由）
- IBKR 断开时自动静默回退下一级数据源，不影响页面运行；持仓同步见下方
- 长桥 (LongPort) 可选行情/历史源：`settings.json` `"use_longport": true` 或子命令 `--longport` 启用；
  默认顺序中 IBKR 在长桥之前，长桥位置可在设置页拖动调整；不可用自动落到下一源（详见下方「长桥 (LongPort) 账户」）
- EODHD 可选付费源 (BYOK)：`eodhd.json` 填 token（或 env `TRACKER_EODHD_TOKEN`）后生效，未配置时
  完全不影响免费链路。默认历史K线 `priority=primary`（在 yfinance 之前，IBKR/长桥之后），
  实时行情 `quotes=fallback`（仅在默认源失败时兜底）、`quotes=primary` 时走 `s=` 批量
  （每批 15 个代码，仍按 ticker 计费）并回落 yfinance；一旦在设置页拖动过顺序，
  显式 `source_order` 优先于 `priority` / `quotes` 的角色（`quotes=off` 仍禁用行情）；
  PB/市值 `fundamentals=fallback`
  只补 yfinance 缺失字段（`primary` 可改为 EODHD 优先）。EODHD 行情不带币种与 PB/市值字段：
  行情币种按本项目代码规范推导（`.IL`/港股美元柜台等可用 `currency_overrides` 覆盖）。
  LSE `.L/.AL` 默认按便士 ÷100，英镑计价标的用 `lse_gbp_symbols` 豁免。详见「配置 EODHD (BYOK)」
- Hyperliquid 永续价为 Binance、Gate.io 之后的第三加密源（markPx 与现货存在基差，仅 USD 系计价代码）

---

## 配置 IBKR 连接

- `ibkr.json` 含连接参数与**交易所映射**（`exchanges`）。Gateway 模式：`"mode": "paper"` 模拟盘（4002）/ `"live"` 实盘（4001），默认 paper；写 `"port"` 可显式指定任意端口（如 TWS 7496/7497）。环境变量 `IBKR_MODE=live` 可临时覆盖
- 配置文件缺字段时自动继承模板/兜底值；所有下划线开头的字段（`_comment` / `_说明`）会被忽略
- 不想在项目目录放配置文件时，可用环境变量指向其他路径：

```bash
IBKR_CONFIG=/data/my-ibkr.json python -m tracker.cli snapshot --ibkr
```

---

## 持仓导入（IBKR / 长桥 / 链上钱包 / 券商文件）

四种来源统一走 `tracker.importer` 管道：CLI `import` 子命令与 Streamlit 侧边栏「导入」页
（各 tab 独立导入）调用同一套采集 + 合并 + 写盘逻辑。

**导入方式**（CLI 与页面一致）：

- **追加合并**（默认）：按代码更新 `quantity`/`avg_cost`，新代码追加，其余持仓保留。
  钱包导入额外带来源标记（链 + 地址）：同一地址重复导入仍是覆盖（幂等），
  不同地址/链的同名代币（如 eth 与 bsc 上的 `USDT-USD`）会累加，不会互相覆盖。
  手工在页面或 `portfolio add` 改写某代码总数量后，该条目的 `source_quantities`
  来源台账会作废（避免下次导入把旧分量加回来），`import_source` 保留：
  同一来源后续导入仍按“覆盖该来源”处理。
- **覆盖**（`--overwrite`）：清空现有持仓后重写

写盘不再自动生成 `.bak`；保留 `base_currency`（大小写不敏感，读取/保存时归一为大写，非法/空值回退 `CNY`）与文件中的其它自定义键；
写盘为原子操作（临时文件 + `os.replace`），中断不会留下半截 JSON；若 `portfolio.json` /
`watchlist.json` 损坏，读取时会尝试同名历史 `.bak` 恢复（如有），仍失败则报可读错误退出（不会静默清空数据）。
`--dry-run` 仅预览不写入；采集结果为空时即使覆盖模式也不会清空文件。

### IBKR 账户

```bash
python -m tracker.cli import ibkr --dry-run          # 预览，仅打印不写入
python -m tracker.cli import ibkr --mode live        # 实盘模式追加导入
python -m tracker.cli import ibkr --overwrite        # 覆盖全部持仓
```

- 自动将 IBKR 账户股票持仓转换为规范代码并写入 `portfolio.json`
- 保留原有 `base_currency`；`avg_cost` 取自 IBKR（合约货币每股均价，含佣金）；英股（LSE 主板/AIM）IBKR 与行情同以便士报价，已自动 ÷100 折成英镑（`.IL` 国际挂盘币种由 IBKR 自报，不折算）
- 无法映射为规范代码的标的（权证/期权/基金等）会跳过并在控制台提示
- 反向映射规则：`SEHK + CNY/CNH → .SS/.SZ`（沪深港通，6 位代码；`8` 开头 5 位为港股人民币柜台，归 `.HK`）；`SHSE + USD → .SS (B股)`；`SZSE + HKD → .SZ (B股)`；`SEHK + HKD/USD → .HK`（USD 为港股美元柜台）；`IBIS/FWB/GETX/SWB + EUR → .DE`（仅认德国交易所，泛欧所落 skipped）；`IOB → .IL`（LSE 国际挂盘，如 `KAP.IL` Kazatomprom GDR；币种 USD/GBP/EUR 混杂，不能落进 GBP/USD 兜底拼出 `KAP.L` 或裸 `KAP`）；`LSE + GBP/GBX → .L`；`TSE + CAD → .TO`；`TSXV + CAD → .V`；`CSE + CAD → .CN`；`NEO/NEOE + CAD → .NE`；`ASX + AUD → .AX`；`TSEJ/XJPX + JPY → .T`（东京；裸 `TSE` 是多伦多，不进东京分支）；`KRX/KSE + KRW → .KS`（KOSPI）；`KOSDAQ/XKOS → .KQ`（必须前置判定，否则 KOSDAQ 落进 KRW 兜底被误归 `.KS`）；`SGX/SES → .SI`（SGD 与 USD 计价证券均归 `.SI`）；`B3/BVMF/BOVESPA + BRL → .SA`；`SMART + USD → 原码`
- 兼容入口：`python -m tracker.cli sync` / `python -m tracker.ibkr_sync`（缺省**追加合并**，与 `import ibkr`/`import file` 一致；`--overwrite` 才覆盖全部，`--append` 为兼容 no-op）
- 所有写入路径（CLI/页面/导入）统一走 `storage.edit_*` 锁内读-改-写：并发保存不会互相覆盖（`flock` 跨进程 + 进程内 `RLock`，锁文件 `<path>.lock` 持久保留）

### 长桥 (LongPort) 账户

```bash
python -m tracker.cli longport-login                  # OAuth 浏览器授权登录（token 缓存于 ~/.longport/，自动刷新）
python -m tracker.cli import longport --dry-run       # 预览长桥账户持仓（港股/美股/沪深）
python -m tracker.cli import longport --overwrite     # 覆盖全部持仓
python -m tracker.cli watchlist import-longport --dry-run  # 预览长桥自选分组导入
python -m tracker.cli watchlist import-longport            # 追加合并到 watchlist.json
python -m tracker.cli watchlist import-longport --overwrite  # 覆盖现有自选
```

**长桥自选导入**：读取账户全部自选分组（QuoteContext.watchlist），每个分组映射为
`watchlist.json` 的一个列表名，代码归一为 Yahoo 规范（`700.HK→0700.HK`、
`600519.SH→600519.SS`、`G13.SG→G13.SI`、`AAPL.US→AAPL`、类别股/优先股
`TAP.A.US→TAP-A` / `WFC.PR.L.US→WFC-PL`）。同一代码在多个分组的 lists 并集合并；
已有条目的阈值/备注保留，仅补列表归属；重复导入幂等。无法映射的标的
（长桥独有 ADR/优先股等 Yahoo 无对应代码）跳过并报告原因。账户里的退市/改码
旧代码（`ERJ→EMBJ`、`BRFS→MBRF3.SA` 等，见 `tracker/symbol_migrations.py`）会自动
迁移到接替代码，无接替码的跳过并说明。

- 认证配置在数据目录下的 `longport.json`（模板 `tracker/templates/longport.example.json` 随包分发，`tracker init` 生成；已 gitignore）：推荐 `auth: oauth`
  （填入 OAuth 客户端注册返回的 `client_id`），也支持 `auth: apikey`（开发者中心 app_key/app_secret/access_token，90 天过期）
- 行情接入：各取数子命令加 `--longport`（如 `quote AAPL --longport`），或 `settings.json` `"use_longport": true`；
  默认顺序中 IBKR 在长桥之前，可在设置页拖动调整；长桥失败自动落到下一源
- 代码映射：内部 `0700.HK` ↔ 长桥 `700.HK`，`600519.SS` ↔ `600519.SH`，`AAPL` ↔
  `AAPL.US`，`D05.SI` ↔ `D05.SG`，类别股/优先股 `TAP-A` ↔ `TAP.A.US` / `WFC-PL` ↔
  `WFC.PR.L.US`；德英加澳日韩/北交所不支持（前置自动跳过，回落默认源）
- UI 登录引导在「导入」页长桥标签（授权在独立子进程运行，状态经 `var/longport_oauth_state.json`）；
  授权子进程独占 `callback_port`（默认 60355），同一端口只能跑一个登录流程

### EODHD (BYOK) 付费源

EODHD 是可选付费数据源，token 自备（免费套餐每天 20 次调用，长期使用需付费）。
**不配置 token 时零影响**，所有取数仍走原有免费链路。

```bash
# 1) 配置 token: 编辑数据目录 eodhd.json（tracker init 会生成模板）
#    或直接注入环境变量（优先级更高，适合容器/CI）:
export TRACKER_EODHD_TOKEN=your_token

# 2) 自检（不写持仓、不污染缓存）
python -m tracker.cli eodhd status
python -m tracker.cli eodhd test AAPL 0700.HK --json

# 3) 之后 quote/history/kline/compare/snapshot/watchlist 会自动按配置接入
python -m tracker.cli kline AAPL --months 12
```

`eodhd.json` 字段（模板见 `tracker/templates/eodhd.example.json`）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；token 为空时仍视为不可用（`tracker eodhd status` 会说明原因） |
| `token` | `""` | API token；env `TRACKER_EODHD_TOKEN` / `EODHD_API_TOKEN` 覆盖 |
| `priority` | `primary` | 历史K线角色：`primary`=在 yfinance 之前（IBKR/长桥之后）/ `fallback`=最后兜底 |
| `quotes` | `fallback` | 实时行情：`off` / `fallback`（默认源失败才用）/ `primary`（EODHD 优先；走 `s=` 批量，每批 15 个代码，批量失败的再回落默认源） |
| `fundamentals` | `fallback` | PB（每股净资产）/市值：`off` / `fallback`（先 yfinance，只补缺失字段）/ `primary`（先用 EODHD `Highlights.BookValue` / `MarketCapitalization`，缺失再补 yfinance） |
| `adjustment` | `adjusted` | `adjusted`=用 adjusted_close（拆股+分红复权）并同比例缩放 OHLC；`raw`=原始成交价 |
| `lse_pence` | `true` | LSE `.L/.AL` 按便士 ÷100 折成英镑 |
| `lse_gbp_symbols` | `[]` | 本身以英镑报价的 LSE 标的（常见于 ETF），不做便士折算 |
| `currency_overrides` | `{}` | 个别标的币种覆盖，如 `{"KAP.IL": "USD"}`（EODHD 行情不返回币种） |
| `timeout` | `10` | 单次 HTTP 超时（秒） |

- 代码映射：Yahoo 规范代码 → EODHD `SYMBOL.EXCHANGE`（`AAPL.US` / `0700.HK` /
  `BP.LSE` / `SAP.XETRA` / `RY.TO` / `BHP.AU` / `9984.TSE` / `005930.KO` / `PETR4.SA` …）；
  未覆盖的市场返回 None，自动跳过（不影响其它源）
- 当前只接全球股票域：A股/B股与加密已有更合适的免费主源，不走 EODHD
- EODHD 实时行情不带名称、PB、总市值：行情币种按代码规范推导（例外用 `currency_overrides`）；
  PB/市值可由 `fundamentals` 使用 EODHD 自带基本面（默认 `fallback` 只补 yfinance 缺失字段，
  结果与其他基本面一样缓存 6h）
- `quotes=primary` 时用 `s=` 参数批量请求（每批 15 个代码），批量未命中/失败的代码自动回落
  yfinance 批量与逐代码源链；EODHD 按 ticker 计费，批量只降低延迟、不减少调用次数
- EODHD 的 `adjusted_close` 会随新分红整体重算：本项目按区间整段重取、不做尾部拼接，
  缓存 TTL 到期后自然刷新

### A股券商文件导入

A股券商无跨券商统一 API（监管对个人程序化接入持续收紧），采用**文件导入**模式：
在券商客户端（同花顺/通达信/华泰/东财/QMT 等）手动导出持仓为 CSV/Excel，本工具解析后映射为 `.SS/.SZ/.BJ` 代码写入 `portfolio.json`。零凭证、跨平台。

```bash
python -m tracker.cli import file 持仓.csv --dry-run   # 预览
python -m tracker.cli import file 持仓.xlsx            # 追加导入
python -m tracker.cli import file 持仓.csv --overwrite --json
```

- 列名模糊匹配（代码/数量/成本列，各家券商叫法不同也能识别）；CSV 自动尝试 utf-8/gbk/gb18030 编码
- 仅识别 6 位 A股代码，非 A股行跳过并提示
- 兼容入口：`python -m tracker.ashare_sync 持仓.csv`（默认追加合并，加 `--overwrite` 覆盖）
- 页面端：「导入」页 → 「券商文件」tab 上传 CSV/Excel 预览后导入

### 链上钱包导入（加密资产）

只读查询 EVM 链上地址余额并写入 `portfolio.json`，无需 API key、不接触私钥：

```bash
python -m tracker.cli import wallet eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 --dry-run
python -m tracker.cli import wallet bsc 0x... --base-currency USDT --json
python -m tracker.cli import wallet tron TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL --dry-run
python -m tracker.cli import wallet solana Hxxxx... --dry-run   # SPL 代币自动取回
python -m tracker.cli import wallet polygon 0x... --tokenlist my_tokens.json
```

- 支持链：`eth` / `bsc` / `polygon` / `arbitrum` / `avalanche` / `tron` / `solana`（每链多个公共节点自动切换；Solana 可用环境变量 `TRACKER_SOLANA_RPC` 前置自定义节点）
- 轻钱包策略：EVM 链主币 `eth_getBalance` + ERC-20 `balanceOf` 批量调用；TRON 走 TronGrid `GET /v1/accounts` 一次取回 TRX + TRC-20；Solana 走 `getBalance` + `getTokenAccountsByOwner`(jsonParsed) 取回 SOL + 全部 SPL（wSOL 并入 SOL），内置主流代币 tokenlist，可用外部 JSON 覆盖
- 不依赖 web3.py/tronpy/solana-py，EVM/Solana 直接 HTTP JSON-RPC，TRON 直接 HTTP
- 兼容入口：`python -m tracker.cli import-wallet`（`--add` 追加写入 / `--overwrite` 覆盖）
- **历史净值**：`python -m tracker.cli wallet-nav <chain> <address> --days 180`
  按链上转账记录回放历史余额（详见「历史净值 → 钱包净值曲线」），不需要先导入 portfolio.json

**稳定币计价 = USD 等价估值**：`BTC-USDT` / `BTC-USDC` / `BTC-FDUSD` 等稳定币计价的代码，
估值口径一律按 USD（`symbols.valuation_currency` 单一权威点，`parse` / 行情源 / 汇率链共用）。
原因：汇率链（CFETS + Yahoo 法币对）没有 `USDT→X` 通道，不归一会让持仓/自选整行拿不到汇率、
被静默排除出视图（总市值/权重/盈亏整体少算）。代价是稳定币脱锚时估值有同幅误差（历史约 3% 级别），
需要精确口径时直接持有 `BTC-USD`。钱包导入的 `--base-currency USDT/USDC` 同样归一为 USD
（否则会生成 `USDT-USDT` 这种任何数据源都没有的自配对）。

---

## Agent / CLI 集成

本仓库的双前端设计：Streamlit 给人看，CLI 给 Agent 用。hermes / openclaw 等手机 Agent 只需执行
`python -m tracker.cli <子命令> --json` 即可获取与页面完全一致的数据（持仓/自选/行情/K线/汇率/导出），
无需打开浏览器。建议 Agent 默认使用 `snapshot --json` 作为「现在怎么样了」的一站式入口。

CLI 不再依赖前端进程做缓存保温：`python -m tracker.cli warm` 可按磁盘持仓/自选预热一次，
`warm --interval 300` 可常驻周期刷新；两者都和页面共用 `var/quotes_cache.db`，也可在启动
`tracker-ui` 后直接受益于前端保温线程写入的缓存。

**给 AI 的完整操作手册**（子命令速查、代码规范、数据源优先级、配置文件字段、常见操作流程、
注意事项）随仓库分发在 [`skills/portfolio-tracker/SKILL.md`](skills/portfolio-tracker/SKILL.md)，
兼容各 agent 的 skill 加载格式（frontmatter + 正文），新对话的 AI 读它即可上手操作本项目。

---

## 已知限制

- 免费数据源：非美股行情普遍延迟 15–30 分钟；无 SLA，偶发限流（已内置批量请求 + 重试缓解）
- akshare 东财 spot 接口对部分数据中心 IP 不友好（本项目所在机器即如此），此时自动回落 Yahoo
- 搜索全部为在线接口（美股/港股：IBKR→东财 suggest→yfinance Search；A股/北交所：东财 suggest；crypto：yfinance Search）；中文查询仅东财 suggest 支持（A股/北交所全量；美股/港股域只认领港股行，故「腾讯」可命中 0700.HK，美股/crypto 直接搜中文名基本无结果）；接口失败即该域空结果 + 合法代码直查
- CFETS 汇率为中间价，与离岸 CNH 有细微差异，组合展示场景可忽略
- `今日估算` 按各持仓 `涨跌幅 × 当前市值` 近似，非精确日内盯市
- 历史净值回填使用**当前持仓数量 × 历史收盘价**，不还原历史调仓；曲线只适合看「一直持有现在的仓位」的近似走势，不能当作真实历史业绩；取不到历史行情的标的（如部分可转债）会被跳过并在 CLI `issues` 中列出，曲线只覆盖有数据的持仓
- 钱包净值 `wallet-nav` 用链上转账回放历史余额：ETH/AVAX/TRON/Solana 零 API Key；Polygon/Arbitrum/BSC 的免费公共浏览器接口不稳定或需要 Etherscan V2 key（可在 `wallet_history.json` 配置）。质押/解冻中的 TRX 不计入；rebasing / fee-on-transfer 代币、浏览器索引缺失、公共节点裁剪历史可能造成偏差；垃圾空投会被忽略，资产数超过 `--max-assets` 时只保留当前估值较大的资产；同名代币（不同链/合约）按 symbol 取主流市场价格，可能与链上实际价值有差异
- A股/港股历史 K 线为前复权（qfq）口径，回填曲线在历史日期的市值/盈亏与当时实际值可能有差异（最新一天一致）；分红/拆股较多的标的历史段仅供看趋势
- IBKR 行情需本机运行 IB Gateway 且 API 已启用；A股/港股/B股数据若无市场数据订阅，`reqTickers` 可能返回空值，自动回退 Yahoo/akshare
- IBKR A股经沪深港通通道：沪 `SEHKNTL` / 科创 `SEHKSTAR` / 深 `SEHKSZSE`，计价 CNH；若你的账户显示不同交易所代码，在 `ibkr.json` 的 `exchanges` 中修改对应键（`CN_SH` / `CN_STAR` / `CN_SZ`）；B股合约根据 IBKR 返回的 `SHSE/USD` 或 `SZSE/HKD` 自动识别
- 长桥行情/持仓需 `pip install longport` 并完成登录；未安装/未配置时 `--longport` 与 `import longport` 报可读错误并回退默认数据源
- EODHD 为 BYOK 付费源：免费套餐 20 次/天；`quotes=primary`/`fundamentals=primary` 都会逐代码消耗调用次数（自选 600+ 时注意额度）；EODHD 行情本身不返回名称/币种/PB/总市值（PB/市值需 `fundamentals` 角色，行情币种用 `currency_overrides` 覆盖例外）；LSE 便士口径可用 `lse_pence` / `lse_gbp_symbols` 调整
- 数据健康只观测**真实回源**（缓存命中不记录）：窗口内无记录只说明这段时间没打上游，不代表数据新鲜；「组合覆盖」以磁盘缓存为准，从未打开过的自选不会因缺缓存报警（持仓缺行情/持仓缺K线会）。`doctor --probe` 是主动联网，单次失败可能只是限流，需结合窗口成功率看趋势；健康事件保留 7 天/最多 2 万条，`cache clear` 不会删除它。只读数据目录/不希望落盘观测时设 `TRACKER_HEALTH_DISABLE=1` 关闭记录

---

## Roadmap

按项目目标排序：

- [x] IBKR 行情接入 + 持仓同步（只读）
- [x] 长桥 (LongPort) 行情/持仓接入（OAuth 登录，可选前置源）
- [x] A股券商持仓文件导入（CSV/Excel）
- [x] 链上钱包余额导入（EVM 五链 + TRON + Solana）
- [x] 多股 K 线对比（同坐标系 + 归一化）
- [ ] **K 线比值模式** — 两标的收盘价比值画成 K 线（如腾讯 / 南非报业）
- [ ] Hyperliquid / A股券商实时**持仓**对接（当前 HL 仅为行情源）
- [ ] 交易流水记录与分批成本（FIFO）
- [ ] 分红/拆分事件跟踪
- [x] 历史净值曲线（按日聚合组合市值；每日快照 + `nav` 子命令 + 组合页走势，回填为当前持仓 × 历史价近似口径）
- [x] 链上钱包净值曲线（`wallet-nav` / 「钱包净值」页：按转账记录回放历史余额，历史币价 + 历史汇率折算）
- [x] EODHD 付费 provider 接入（BYOK token 自备；历史K线 primary、行情 fallback，LSE 便士/复权口径可配）
- [x] 数据健康观测（`doctor` / 「数据健康」页：回源成功率·耗时·最近失败 + 持仓/自选缓存缺口 + 本地完整性；`--probe` 主动探测）
- [x] 线上数据契约测试（各域真实行情/历史/指数/汇率字段契约，`pytest -m network` 显式运行）

---

## 打包与分发

项目是标准 Python 包（`pyproject.toml`，setuptools 后端），wheel / sdist / Docker 三种形态：

```bash
# 构建 (需 python -m build; 产物在 dist/)
python -m build

# 安装 (任选其一)
pip install dist/madeleine_tracker-*.whl            # 核心: CLI + 取数
pip install "dist/madeleine_tracker-*.whl[ui]"      # + Streamlit UI / plotly
pipx install "madeleine-tracker[ui]"                # pipx: 自动隔离 venv, 命令全局可用

# Docker (UI 容器, 数据落 /data 卷)
docker build -t madeleine-tracker .
docker compose up -d --build            # http://localhost:8501
docker compose exec tracker tracker init   # 首次使用: 生成配置模板
```

分发设计要点：

- **依赖拆 extras**（`pyproject.toml`）：核心只装 `openbb/akshare/pandas`；`[ui]` 才拖
  streamlit/plotly；`[ibkr]`/`[longport]` 是可选增强（代码里本就惰性导入，未装自动回退）。
  Agent 用户装核心即可，不必拖 ~1GB 的 UI 依赖
- **两个 console scripts**：`tracker`（= `python -m tracker.cli`）与 `tracker-ui`
  （= `streamlit run tracker/ui/app.py`，自动定位包内 app.py + 落深色主题）
- **数据目录与安装位置解耦**（`tracker/paths.py`）：`TRACKER_HOME` > 仓库根 > 平台用户目录；
  pip 安装后用户数据/缓存/图表全部落平台目录，升级重装不丢；`tracker init` 从包内模板
  （`tracker/templates/`，随 wheel 分发）生成初始配置
- **wheel 内容**：仅 `tracker` 包 + 模板 + vendored lightweight-charts JS；sdist 额外带
  tests / skills / docs（MANIFEST.in）；用户数据文件（portfolio.json 等）一律不进包
- **Docker**：非 root 运行，`TRACKER_HOME=/data` 挂卷持久化，`--build-arg EXTRAS=ui,ibkr,longport`
  可选带券商依赖；健康检查打 `/_stcore/health`

---

## Madeleine Portfolio Tracker (English)

> **Local-first multi-market portfolio tracker. Human-facing Streamlit UI + agent-facing CLI (`--json`). Zero API keys. Completely free.**

A **local-first** portfolio tracker supporting **A-shares / B-shares / Beijing SE / HK / US / DE / GB / CA / AU / SG (SGX) / BR (B3)** and **crypto**. Data layer is split into four isolated **providers** (global stocks / CN stocks / crypto / macro-risk indices) that only meet at the portfolio & watchlist aggregation level.

### Goals

1. Multi-source portfolio tracking (A/B/Beijing SE/HK/US/DE/GB/CA/AU/SG/BR + crypto)
2. Watchlist management with two-level price / PB / market-cap threshold alerts
3. Look up K-line & fundamentals by symbol **or name** (fuzzy search)
4. Dual frontends — Streamlit for humans, unified JSON CLI for agents (hermes / openclaw)
5. Read-only broker integration — IBKR Gateway, LongPort (OAuth login), A-share broker file import, Hyperliquid quotes
6. K-line comparison (normalized overlay and optional ratio line, done) and **ratio-as-candles** (planned)

### Features

- **Multi-currency portfolio tracking** — automatic FX conversion, unified base currency
- **Watchlist with price / PB / market-cap threshold alerts** — two-level upper/lower thresholds, severity levels, distance prediction
- **IBKR integration** — TWS / IB Gateway snapshots + position sync, automatic fallback
- **LongPort integration** — optional quotes/history/positions source (OAuth login), silent fallback to default sources
- **EODHD BYOK (optional paid source)** — bring your own token; EOD history primary by default (split+dividend adjusted), quotes fallback-only by default (`quotes=primary` batches 15 symbols per `s=` request); PB/market-cap can use EODHD fundamentals (`fundamentals=fallback` fills only missing fields); LSE pence handling, per-exchange ticker mapping, zero impact when unconfigured
- **K-line charts** — daily/weekly/monthly, MA overlays, volume, MACD/RSI/KDJ/Bollinger; slide mode loads full listing history by default
- **Macro/risk index K-lines** — dedicated `IX.<KEY>` symbol space (USD index, VIX, CSI 300, commodity main-continuous futures — gold, silver, eggs, soybean meal, soybeans, hogs, lithium carbonate — uranium spot/long-term (Cameco monthly), cement indices, ...), separate page & `index-kline` subcommand, dedicated provider source chain
- **Multi-symbol comparison** — same-coordinate overlay normalized to 100, optional ratio line with history-FX adjustment; plus a crypto-only sub-tab on the K-line page (independent per-coin candle panes, raw prices, shared time axis)
- **Fundamentals** — valuation sub-view for any A-share/HK/US stock: latest PE(TTM), PB, dividend yield and 5-year average yield, plus historical PE/PB curves and percentile (Baidu via akshare); the AH-premium sub-view retains the original A+H curve: A-share close ÷ (H-share close × historical HKD/CNY FX) − 1, selectable pair/range, 0% parity reference line, range high/low/mean/percentile summary (dedicated `/fundamentals` page)
- **Financial calendar** — earnings, scheduled disclosures, ex-dividend/record dates for holdings & watchlist over the current and next months (yfinance per-symbol + akshare bulk, optional LongPort fallback), dedicated page & `calendar` subcommand; export to iCalendar (`.ics`) for Apple Calendar / Google Calendar / Outlook; only probabilistically relevant A-share report periods/boards are fetched, requests run concurrently, and a process-level warmer (also exposed as `tracker warm`) keeps the disk cache fresh for the default scope
- **Historical NAV** — daily portfolio snapshots in an isolated SQLite DB (`nav_history.db`, separate from the quote cache), NAV + cost chart on the portfolio page, `nav --save` / auto-upsert on every successful portfolio render (same-day overwrite, corrected by background refreshes), and `nav --backfill` (current holdings × historical closes, historical FX converted; approximate, no historical position changes)
- **Unified import pipeline** — IBKR account / LongPort account / on-chain wallet (5 EVM chains + TRON + Solana) / A-share broker CSV-Excel file, each with append-merge or overwrite mode; same logic drives the Streamlit「导入」page
- **Wallet NAV curve** — `wallet-nav` / the 「钱包净值」 page replays on-chain transfers to recover historical balances, then values them with historical crypto prices and FX; zero API key on Ethereum / Avalanche / TRON / Solana, optional Etherscan V2 key for Polygon / Arbitrum / BSC
- **Unified CLI** — 23 subcommands (snapshot, nav, wallet-nav, quote, watchlist, portfolio, report, export, fx, history, kline, calendar, search, compare, index-kline, import, longport-login, eodhd, cache, warm, init; `sync` / `import-wallet` kept as compat aliases); all support machine-readable output (`--json`, or `-f json` for report/export). `warm` preheats/keeps the quote, FX and calendar caches fresh without requiring the Streamlit process (`--interval` for a resident warmer)

### Quick Start

```bash
# Option A: pip install (recommended — gives you the `tracker` / `tracker-ui` commands)
pip install "madeleine-tracker[ui]"     # core + Streamlit UI; [all] adds IBKR/LongPort
tracker init                            # first run: copy packaged templates to the data dir

# Option B: from source (development)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt         # == pip install ".[all,dev]"

# Streamlit dashboard
tracker-ui                              # installed; or: streamlit run tracker/ui/app.py (source tree)

# CLI examples (installed: `tracker ...`; source tree: `python -m tracker.cli ...`)
tracker snapshot --json
tracker snapshot --save                 # also persist today's NAV snapshot
tracker nav --days 90                   # historical NAV table
tracker nav --backfill --days 365       # approximate backfill from current holdings
tracker wallet-nav eth 0xd8dA...6045    # replay on-chain transfers into a wallet NAV curve
tracker quote AAPL 600519.SS BTC-USD
tracker kline AAPL --months 12
tracker import ibkr --dry-run
tracker eodhd status                    # optional paid source (BYOK); no-op unless token is set
tracker import file positions.csv --dry-run
tracker longport-login && tracker import longport --dry-run
```

User data (`portfolio.json`, `watchlist.json`, `settings.json`, quote cache, generated
HTML) lives in the **data dir** — `$TRACKER_HOME`, else the repo root when running from
source, else the platform user dir (`~/.local/share/tracker` on Linux). See `tracker/paths.py`.

### Data Source Fallback

Quotes are fetched with a priority chain:

1. **IBKR** (TWS / IB Gateway) — real-time if subscribed, delayed otherwise, batch snapshot (default order ahead of LongPort; draggable)
2. **LongPort** (optional, `use_longport` / `--longport`) — HK/US/CN quotes & history, silent fallback
3. **EODHD** (optional BYOK, `eodhd.json` / `TRACKER_EODHD_TOKEN`) — history primary by default (ahead of yfinance, behind IBKR/LongPort), live quotes fallback-only by default; errors fall through to the next source
4. **yfinance** (via OpenBB) — batch quote (1 request) + individual retry
5. **akshare** (East Money) — A-shares / HK spot + history (CN domain: akshare first)
6. **FX** — CFETS for full XXX/CNY table, direct/inverse/USD bridge fallback
7. **Indices** — `IX.<KEY>` historical only: CN indices via akshare (Sina), US SPX/NDX/DJI via yfinance with Sina fallback, cement / commodity-futures / Cameco uranium indices on dedicated sources (no fallback)
8. **Crypto** — Binance → Gate.io → Hyperliquid perpetuals (USD-quoted only) → yfinance; reorder with `settings.json` `crypto_source` or `--crypto-source`

The per-domain quote/history fallback order is editable by drag-and-drop on the Settings page
(quotes and history are separate lists for global / CN / crypto / index), or directly under
`settings.json` → `source_order`. The list is authoritative; sources omitted from a hand-written
list are appended at the end. IBKR and LongPort participate in the same ordering — their toggles
only control whether they are enabled, not their position.

IBKR/LongPort disconnects silently fall back to the next source; the page continues running without interruption.

### Project Structure

```
tracker/ui/        Streamlit dashboard (app.py + cli_main() console entry + kline/index/fundamentals/calendar/import/settings pages + longport login + settings)
tracker/           Core package: paths.py (data-dir resolution), cli/ subcommands (incl. init, warm),
                   providers/, symbols, symbol_migrations, storage, prices, fx, search,
                   cache, charting, analytics, watchlist, services/ (incl. warm.py),
                   importer, ibkr, longport, longport_oauth, eodhd, ibkr_sync, ashare_sync, wallet
tracker/templates/ Packaged config templates + streamlit theme (shipped in the wheel; `tracker init` copies them into the data dir)
tracker/assets/    Vendored lightweight-charts standalone JS (offline, no extra dependency)
pyproject.toml     Packaging metadata (dependency extras, console scripts, package-data)
Dockerfile         Container image for the UI (data in the /data volume; docker-compose.yml included)
portfolio.json     Holdings config (editable via page; lives in the data dir)
watchlist.json     Watchlist config (editable via page; lives in the data dir)
settings.json      Display & data-source settings (lives in the data dir)
ibkr.json          IBKR config (gitignored; template in tracker/templates/)
longport.json      LongPort config (gitignored; template in tracker/templates/)
eodhd.json         EODHD BYOK config (gitignored; template in tracker/templates/; token can also come from TRACKER_EODHD_TOKEN)
tests/             Unit tests (offline, mocked)
```

### License

MIT — see [LICENSE](LICENSE)
