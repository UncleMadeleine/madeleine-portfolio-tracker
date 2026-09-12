# A股券商持仓导入可行性调研报告

> 分支：`feature/ashare-broker-import`
> 日期：2025-09-13
> 背景：项目已支持通过 IBKR（Interactive Brokers）的 TWS/IB Gateway API 同步真实持仓
> （见 `tracker/ibkr.py` / `tracker/ibkr_sync.py`）。本报告调研能否类似地支持 A股券商
> （东方财富/同花顺/华泰等）的持仓数据导入。

---

## 一、结论摘要

| 维度 | 结论 |
|------|------|
| **能否像 IBKR 那样通过本地 API 自动同步 A股持仓？** | **不能直接复刻**。A股没有 IBKR 那样跨券商、跨平台的统一只读 API。 |
| **最可行方案** | **券商客户端导出文件（CSV/Excel）解析导入**。零凭证、零依赖、跨平台、无合规风险，与项目"本地优先、零 API Key"理念一致。 |
| **次选方案（需条件）** | **miniQMT / xtquant**（券商官方量化接口）。官方合规、可自动读取持仓，但仅 Windows、需开通量化权限、需客户端常驻登录。 |
| **不推荐** | easytrader（GUI 自动化）、emtl/emta（逆向网页接口）。均需登录凭证、Windows 限定、合规灰色。 |
| **akshare** | **无个人持仓查询能力**，仅提供市场行情/财务数据，无法用于持仓导入。 |

**推荐落地**：实现 `tracker/ashare_sync.py`，支持解析各券商客户端导出的持仓 CSV/Excel
文件，映射为 Yahoo 代码后写入 `portfolio.json`。本 worktree 已附带原型实现。

---

## 二、现有 IBKR 接入模式回顾

IBKR 的接入之所以顺畅，关键在于：

1. **统一 API**：IB Gateway / TWS 提供跨市场的统一只读 API（`ib_async`），一次连接
   即可拉取全市场持仓、行情、历史。
2. **本地只读**：`readonly=True` 接入，不涉及交易；配置文件 `ibkr.json` 被 gitignore，
   且 Gateway 本机默认无需在代码中写死凭据。
3. **代码映射**：`ibkr_to_yahoo()` 将 IBKR 合约映射为 Yahoo 后缀（`.SS/.SZ/.BJ/.HK` 等）。

A股券商生态完全不同：**没有跨券商的统一 API**，每家券商各自为政，且监管对个人
程序化接入持续收紧。因此无法 1:1 复刻 IBKR 模式，需降级为"文件导入 + 可选 API 适配"。

---

## 三、各方案详细调研

### 方案 A：akshare（❌ 不可行 — 无个人持仓接口）

akshare 是项目已依赖的行情库（`tracker/prices.py` 中 `_akshare_quote` / `_akshare_history`），
但经核查其 1000+ 接口清单：

- `stock_account_statistics_em`：东方财富-股票账户**统计**数据（全市场开户数等宏观统计），
  **非个人账户持仓**。
- `broker_positions` / `variety_positions`：期货**席位**持仓（前20会员排名），非个人持仓。
- `stock_institute_hold`：机构持股，非个人持仓。

**结论**：akshare 定位是公开市场数据爬虫，明确声明"无交易执行能力，不具备下单、
账户管理等功能"。**无法用于个人持仓导入**。

### 方案 B：东方财富（⚠️ 有接口但需凭证/合规风险）

#### B1. 官方掘金量化（gm SDK）— 官方但门槛高
- 东财掘金量化终端提供 `get_position(account_id)` 查询持仓（见
  `emquant.18.cn/help/doc/python/trade_data.html`）。
- **需开通量化账户**，通过其终端运行策略，属官方合规通道。
- 门槛：需申请、资产要求、终端常驻。对"本地追踪器"场景过重。

#### B2. 非官方逆向库 emtl / emta — 不推荐
- `riiy/emtl`（2024，73 star）及其 fork `zsmatrix62/emtl`：逆向东方财富网页交易端
  `jywg.18.cn`，提供 `query_asset_and_position()`。
- `emta`（v0.5.0）：类似，依赖 `ddddocr` 自动识别验证码。
- **安全风险**：需传入**明文用户名 + 密码**（`client.login("username", "password")`），
  密码经 `emt_trade_encrypt` 加密后 POST 到券商服务器，验证码靠 OCR 绕过。
- **合规风险**：逆向券商网页接口，违反服务条款；2024 年两协会《证券公司交易信息系统
  接入管理规范》严禁个人"类外接"方式，券商正在清退违规接入。
- **维护性**：非官方，券商改版即失效。

### 方案 C：同花顺（⚠️ 有导出/自动化但受限）

#### C1. 客户端导出（✅ 通用）
- 同花顺 PC/APP 均支持导出持仓明细为 Excel/CSV（交易→持仓→导出）。
- 字段通常含：证券代码、证券名称、持仓数量、成本价、现价、盈亏等。
- **无需凭证、无需 API**，是最稳妥的通用数据源。

#### C2. easytrader GUI 自动化（❌ 不推荐）
- `shidenggui/easytrader`（10K star）通过 `pywinauto` 操控同花顺/华泰/银河客户端 GUI，
  `user.position` 可读取持仓（经剪贴板或 xls 文件策略）。
- **限制**：
  - **仅 Windows**（GUI 自动化）。
  - 需客户端运行并登录（通用同花顺不支持自动登录，需手动登录一次）。
  - 需账户号/密码/通讯密码（华泰需 `comm_password`）。
  - 部分客户端限制剪贴板拷贝，需切换 `grid_strategies.Xls` 文件策略。
  - 脆弱：客户端 UI 改版即失效，验证码弹窗会中断。
- **合规**：GUI 自动化属"类外接"灰色地带，监管收紧中。
- **维护**：主仓近一年有 PR 合入（2024-10 ~ 2025-10），但核心同花顺适配仍依赖老版本客户端。

### 方案 D：华泰 / 国金等 — miniQMT / xtquant（⚠️ 官方但 Windows 限定）

- miniQMT 是券商官方低门槛量化接口（基于迅投 QMT），`xtquant` Python 库提供
  `query_stock_positions(acc)` 查询持仓（含 `volume`/`avg_price`/`market_value`）。
- 华泰、国金、银河等多家券商提供 QMT 终端，easytrader 也已集成 miniqmt 适配。
- **限制**：
  - **仅 Windows**（MiniQMT 客户端是 Windows 桌面软件，不支持 Mac/Linux）。
  - 需向券商**申请开通 QMT 权限**（通常有资产门槛，如 50 万）。
  - 需 QMT 客户端常驻登录，`xtquant` 通过本地 TCP 连接客户端。
  - `xtquant` 库 100MB+，需单独安装。
- **跨平台变通**：社区有 `xqshare` / `qmt-bridge` / `qmtlink` 等方案，在 Windows 上跑
  HTTP 代理服务，Mac/Linux 远程调用。但引入额外部署复杂度。
- **合规**：官方通道，合规。但仅适合已有 QMT 账户的 Windows 用户。

### 方案 E：通用文件导入（✅ 强烈推荐）

几乎所有 A股券商客户端（同花顺、通达信、华泰专业版、东方财富、QMT）都支持
**导出持仓为 CSV/Excel**。这是唯一同时满足以下条件的方案：

- ✅ **零凭证**：不触碰任何登录密码。
- ✅ **跨平台**：纯文件解析，Linux/Mac/Windows 均可。
- ✅ **零新增依赖**：仅需 `pandas`（项目已有），读 Excel 需 `openpyxl`（可选）。
- ✅ **无合规风险**：用户主动导出自己的数据，不涉及接口逆向或外接。
- ✅ **券商无关**：一套解析器适配多券商，按列名模糊匹配。
- ✅ **与项目理念一致**：本地优先、零 API Key、配置为可编辑文件。

**代价**：非全自动，需用户手动导出一次文件（可定期导出）。

---

## 四、方案对比表

| 方案 | 可行性 | 自动化 | 维护性 | 安全性 | 合规 | 依赖/平台 | 凭证需求 |
|------|--------|--------|--------|--------|------|-----------|----------|
| **A. akshare** | ❌ 无个人持仓接口 | — | — | — | ✅ | 已有 | 无 |
| **B1. 东财掘金 gm SDK** | ⚠️ 官方但门槛高 | 高 | 中 | ⚠️ 需账号 | ✅ | gm SDK + 终端 | 需量化账户 |
| **B2. emtl/emta 逆向** | ⚠️ 可用但脆弱 | 高 | 低（改版即失效） | ❌ 明文密码 | ❌ 违反条款 | ddddocr | 用户名+密码 |
| **C1. 同花顺导出文件** | ✅ | 手动 | 高 | ✅ | ✅ | 无 | 无 |
| **C2. easytrader GUI** | ⚠️ Windows 限定 | 高 | 低（UI 脆弱） | ❌ 密码/通讯密码 | ⚠️ 灰色 | pywinauto + Win | 账号+密码 |
| **D. miniQMT/xtquant** | ⚠️ 官方但 Win 限定 | 高 | 中 | ⚠️ 需登录 | ✅ | xtquant + Win 客户端 | QMT 账户登录 |
| **E. 通用文件导入** | ✅ | 手动 | 高 | ✅ | ✅ | pandas（已有） | 无 |

---

## 五、推荐方案与实现步骤

### 推荐方案：E（通用文件导入）为主，D（miniQMT）为可选未来扩展

**理由**：
1. 本项目是**只读追踪器**（IBKR 接入也是 `readonly=True`），核心诉求是"把真实持仓
   同步进 portfolio.json"，而非自动交易。文件导入完全满足。
2. 项目理念是"零 API Key、本地优先、跨平台"。文件导入是唯一不破坏该理念的 A股方案。
3. A股券商无统一 API，文件导入是唯一券商无关方案，一套代码覆盖所有券商。
4. miniQMT 虽官方合规，但 Windows 限定 + 量化权限门槛，与项目跨平台定位冲突，适合作为
   高阶用户的可选适配器（类似 `ib_async` 可选依赖模式），未来按需添加。

### 实现步骤（已在本 worktree 完成原型）

1. **新建 `tracker/ashare_sync.py`**：仿 `ibkr_sync.py` 的 CLI 模式
   （`--portfolio` / `--dry-run` / `--json`），解析券商导出文件。
2. **代码映射**：复用 `ibkr.py` 中 A股前缀规则（`5/6/9→.SS`、`4/8/920→.BJ`、
   其余→`.SZ`），将 6 位证券代码转为 Yahoo 后缀。
3. **列名模糊匹配**：各券商导出列名不一（"证券代码"/"股票代码"/"代码"，
   "持仓数量"/"当前持仓"/"股份余额"，"成本价"/"参考成本价"/"买入均价"），
   用候选键列表逐个匹配，兼容同花顺/通达信/华泰/东财/QMT 等常见格式。
4. **支持 CSV / Excel**：CSV 用 pandas 内置；Excel 需 `openpyxl`（惰性导入，缺失时提示）。
5. **安全写入**：写入前备份 `portfolio.json` 为 `.bak`，保留 `base_currency`。
6. **零新增硬依赖**：仅用 `pandas`（已有）；Excel 支持为可选，缺失 `openpyxl` 时
   优雅降级并提示安装。

### 未来可选扩展（不在本次原型范围）

- **miniQMT 适配器**：为已有 QMT 账户的 Windows 用户添加 `xtquant` 可选依赖适配，
  仿 `ibkr.py` 的惰性导入 + 不可用回退模式。
- **更多券商格式**：根据用户反馈扩充列名候选键与代码映射规则。

---

## 六、合规与安全说明

### 涉及登录凭证的方案（已在表中标注 ❌）

- **emtl / emta / easytrader**：需用户提供券商**用户名、密码、通讯密码**。
  - 🔴 **安全风险**：明文密码经代码处理，存在泄露风险；本项目配置文件虽 gitignore，
    但凭证进入本地文件仍有风险。
  - 🔴 **合规风险**：逆向券商接口 / GUI 自动化属"类外接"，2024 年起监管明确严禁，
    券商正在清退。可能导致账户被限制。
  - **本原型不采用这些方案。**

### 不涉及凭证的方案

- **文件导入（推荐）**：用户自行在券商客户端导出文件，本工具只读解析，不接触任何
  登录信息。导出文件本身含持仓数据，建议用户妥善保管，勿随意分享。
- **miniQMT（未来可选）**：用户在券商官方客户端登录，本工具通过本地 TCP 读取，
  不经手密码；属官方合规通道，但需用户自行开通 QMT 权限并承担客户端登录责任。

### 数据安全建议

- 导入的持仓文件可能含股东代码、成本等敏感信息，应在文档中提示用户：
  - 导出文件用完即删，或存放于 gitignore 目录。
  - 不要将导出文件提交到版本库。
  - `portfolio.json` 本身已被项目视为本地配置（参考 `ibkr.json` 的 gitignore 处理）。

---

## 七、参考链接

- akshare 接口清单：https://github.com/akfamily/akshare/blob/main/docs/tutorial.md
- 东财掘金量化 get_position：https://emquant.18.cn/help/doc/python/trade_data.html
- emtl（逆向东财）：https://github.com/riiy/emtl 、https://github.com/zsmatrix62/emtl
- emta：https://pypi.org/project/emta/
- easytrader：https://github.com/shidenggui/easytrader 、https://easytrader.readthedocs.io/
- miniQMT / xtquant：https://www.miniqmt.com/pages/docs/xttrader.html
- easytrader miniqmt 集成：https://github.com/shidenggui/easytrader/blob/master/docs/miniqmt.md
- miniQMT 跨平台代理：https://github.com/georgezhu08/xqshare 、https://github.com/atompilot/qmt-bridge
- 监管收紧个人程序化接入：https://m.cls.cn/detail/2480831
- 掘金量化 CSV 文件单格式：https://www.myquant.cn/docs/subject/scanner_v2_csv_guide
