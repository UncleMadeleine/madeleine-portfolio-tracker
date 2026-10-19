---
name: portfolio-tracker
description: Use when the user asks to check or manage their investment portfolio, holdings, watchlist, stock prices, price-threshold alerts, exchange rates, IBKR sync, Longport (长桥) quotes/positions/login, or quote cache in this repo (madeleine-portfolio-tracker). Trigger keywords: 持仓, 组合, 自选, watchlist, 股票, 行情, 报价, quote, 汇率, fx, IBKR, 长桥, longport, 快照, snapshot, 缓存, K线, kline, 指数, index, 导入, import, 搜索, search, 对比, compare. Operate it through the CLI `python -m tracker.cli <子命令>` (alias `python -m tracker`). Also covers editing portfolio.json / watchlist.json / ibkr.json / longport.json and the Yahoo symbol conventions.
---

# Madeleine Portfolio Tracker CLI

本地多市场投资组合追踪 (A股/B股/北交所/港股/美股/德股/英股/加股/澳股/新加坡股/巴西股 + 加密货币 + 宏观/风险指数)。
数据层抽象为 **provider 层** (`tracker/providers/`): 四个市场域各一个 provider —
全球股票 (美股/港股, 长桥→IBKR→yfinance→港股 akshare 兜底)、A股/B股 (.SS/.SZ/.BJ, akshare 固定优先→yfinance)、
加密货币 (`BASE-QUOTE` 连字符代码, Binance 优先→Gate.io→Hyperliquid(仅USD系)→yfinance; 数据源偏好 settings.json `crypto_source` 或 CLI `--crypto-source`, 默认 auto=默认链, 指定源前置失败自动回落),
指数 (`IX.<KEY>`, 中国指数 akshare 优先, 其余 yfinance→akshare 新浪兜底; 水泥网/商品期货/恒生官网指数为专属源无兜底)。
各域只在投资组合/watchlist 聚合, 数据层面严格分域。有 Streamlit 页面
(`python -m streamlit run tracker/ui/app.py`, 5 页: 组合 `/` · K线 `/kline` · 指数K线 `/index` · 导入 `/import` · 设置 `/settings`)
和命令行两套入口。**AI 操作请优先用 CLI。**

所有子命令都支持机器可读输出: 多数是 `--json`; `report`/`export` 用 `-f json`
(report 默认 md, export 默认 json)。无表格花哨格式, 便于解析。

## 运行入口

```bash
# 源码树 (仓库根目录)
cd ~/Project/madeleine-portfolio-tracker
.venv/bin/python -m tracker.cli <子命令> [选项]     # 统一入口
.venv/bin/python -m tracker <子命令>               # 等价别名

# pip 安装后 (pip install madeleine-tracker / pipx): 直接有 tracker 命令,
# 数据落平台用户目录 (见「配置文件」一节), 无需进入仓库目录
tracker <子命令> [选项]
tracker init        # 首次使用: 生成配置模板 (数据目录下)
tracker-ui          # pip 安装后起 Streamlit 页面 (等价 streamlit run tracker/ui/app.py)
```

## 子命令速览

| 子命令 | 作用 | 常用示例 |
|---|---|---|
| `snapshot` | 持仓+自选快照(含阈值提醒) | `snapshot --json`, `snapshot --base USD`, `snapshot --watchlist 科技` |
| `quote` | 实时行情 | `quote AAPL 600519.SS` |
| `watchlist` | 自选管理 list/add/remove/import-longport | `watchlist list`, `watchlist add TSLA --list 科技 --upper1 420`, `watchlist import-longport --dry-run` (长桥自选导入, 分组→列表, 代码归一) |
| `portfolio` | 持仓管理 list/add/remove/set-base | `portfolio list`, `portfolio add AAPL --quantity 10 --avg-cost 180`, `portfolio set-base USD` |
| `fx` | 汇率 | `fx USD CNY` |
| `history` | 历史价格 | `history AAPL --months 12` |
| `kline` | K线蜡烛图 (HTML+摘要) | `kline AAPL --months 6 --ma 5,20,60`, `kline 0700.HK --period weekly --open`, `--json` 出数据, `--refresh` 强刷 |
| `search` | 代码/名称搜索→规范代码 | `search 腾讯 --json`, `search 700 --limit 5`, `search AAPL --exact` |
| `compare` | 多股走势对比 (归一化) | `compare AAPL 0700.HK 600519.SS`, `compare AAPL NVDA --json`, `--raw` 不归一化 (原币种) |
| `index-kline` | 宏观/风险指数K线 (IX.<KEY>) | `index-kline IX.DXY --months 12`, `index-kline IX.CSI300 --list` (指数默认无成交量副图, `--volume` 才显示) |
| `import` | 统一持仓导入 (ibkr/longport/wallet/file) | `import ibkr --dry-run`, `import longport --dry-run`, `import wallet eth 0x... --dry-run`, `import file 持仓.csv --dry-run` |
| `longport-login` | 长桥 OAuth 登录 (浏览器授权) | `longport-login` (token 缓存于 ~/.longport/openapi/tokens/<client_id>, 自动刷新), `longport-login --json` 只出授权 URL (供脚本), `-o <文件>` 写 URL 到文件 |
| `report` | 自选监控阈值报告 | `report -f json -o report.json`, `report -f csv -w 科技` |
| `export` | 组合快照导出 | `export -f md -o snapshot.md`, `export -f json` |
| `sync` | IBKR 同步 (兼容入口→import ibkr; **注意 sync 缺省覆盖全部持仓**, `--append` 才追加, 与 import ibkr 相反) | `sync --dry-run` |
| `import-wallet` | 链上钱包导入 (兼容入口→import wallet; 缺省只查询, `--add`/`--overwrite` 才写盘) | `import-wallet eth 0x... --add` |
| `cache` | 行情磁盘缓存 | `cache info`, `cache clear` |
| `init` | 首次使用: 模板落地到数据目录 | `init`, `init --force` |

`--help` 查看每个子命令的完整参数: `python -m tracker.cli snapshot --help`; `-v/--version` 看版本。
`kline` / `compare` 接受宽松输入 (中文名/裸数字/非规范代码), 自动搜索回退到规范代码。
`-w`/`--list` (report/export/watchlist) 支持逗号分隔多列表 (取并集)。
取数子命令 (snapshot/quote/history/kline/compare/watchlist/report/export) 均有数据源开关
`--akshare` / `--ibkr` / `--longport` / `--crypto-source`, 语义一致 (指定源前置, 失败回落默认链)。

## 代码规范 (自有后缀规范 + 内部 type 字段)

后缀规范是**本项目自定义的** (恰好与 Yahoo Finance 兼容), provider 按它适配我们的代码;
域的**权威判据是内部 `type` 字段** (`global`/`cn`/`crypto`, 指数域为 `index`), 由系统在所有写入路径
(portfolio/watchlist 保存、IBKR/A股/钱包导入、搜索结果) 自动推导和覆写 —— 用户不可见、不可改,
手改 JSON 里的 type 会在下次保存时被纠正。路由按 `type` 走, 不在取数时重复猜后缀。

| 市场 | 后缀 | 示例 | 币种 | type | 说明 |
|---|---|---|---|---|---|
| 美股 | 无 | `AAPL` | USD | global | 含类别股 `BRK-B` / 优先股 `WFC-PL` |
| A股 | `.SS`/`.SZ` | `600519.SS` `000001.SZ` | CNY | cn | `.SH` 别名自动归一为 `.SS` |
| B股 | `.SS`/`.SZ` | `900902.SS` `200012.SZ` | USD / HKD | cn | 沪 B 美元 / 深 B 港币 |
| 北交所 | `.BJ` | `830799.BJ` | CNY | cn | |
| 港股 | `.HK` | `0700.HK` (4位补零) | HKD / CNY / USD | global | 多柜台: 人民币柜台 `8` 开头 5 位码以 CNY 计价 (如 `82318.HK` 中国平安-R); 美元柜台只属于 ETF 三柜台, 代码 `09xxx`/`41xxx` (如 `9804.HK` PP越南-U / `09070.HK` 平安香港高息-U / `09188.HK` 华夏沪深三百-U) 以 USD 计价。美元柜台币种由数据源自报, 不能按代码形态推导 (`normalize` 剥前导零后 `9070.HK` 与 `9988.HK` 阿里同形) |
| 德股 | `.DE` 等 | `SAP.DE` | EUR | global | `.F`/`.BE`/`.DU`/`.HM`/`.MU` |
| 英股 | `.L` | `BP.L` | GBP | global | Yahoo 报价为便士, 系统自动 ÷100; `.IL`=LSE 国际挂盘 (IOB, 如 `KAP.IL` Kazatomprom GDR, **USD 计价**), `.AL` 同属伦交所 |
| 加股 | `.TO` 等 | `RY.TO` | CAD | global | `.V`/`.CN`/`.NE` |
| 澳股 | `.AX` | `BHP.AX` | AUD | global | |
| 新加坡股 | `.SI` | `D05.SI` | SGD / USD | global | `.SG` 别名自动归一为 `.SI` (yfinance 无 `.SG` 数据); SGX 有 SGD 与 USD 计价证券 |
| 巴西股 | `.SA` | `PETR4.SA` | BRL | global | B3; IBKR 交易所代号 `B3` (旧 `BVMF` 已下线) |
| 加密货币 | `-QUOTE` | `BTC-USD` `ETH-USDT` | 计价货币 | crypto | 无分隔符 `BTCUSD` 自动补全 |
| 指数 | `IX.<KEY>` | `IX.DXY` `IX.VIX` `IX.CEMPI` `IX.LC` `IX.HSAHP` | 各异 | index | 已收录 38 个, `index-kline --list` 可查 |

- 推导唯一入口: `symbols.type_for_symbol()`; 单字母连字符是美股类别股 (`BRK-B` → global),
  `P+系列字母` 连字符 (`WFC-PL`) 是美股优先股, 都不是 crypto。
- 加密货币无分隔符写法 (`BTCUSD`/`ETHUSDT`/`BTCFDUSD`) 保存时自动补全为 `BASE-QUOTE`。
- B股: 沪 B (900xxx.SS) 以**美元**交易, 深 B (200xxx.SZ) 以**港币**交易, `avg_cost` 按实际交易币种。
- 英股 `.L`/`.AL` (LSE 主板/AIM): IBKR 与 Yahoo 均以**便士**报价, 行情与 IBKR 导入的 `avg_cost` 都由系统 ÷100 折成英镑; `.IL` (IOB) 币种由数据源自报 (KAP.IL=USD), 不折算。
- 测试用例、搜索结果、IBKR 导入 (`ibkr_to_yahoo`) 产出的代码均符合本规范。
- 无法识别的后缀报 `无法识别的交易所后缀`; 非法连字符代码报 `无法识别的代码`。

### 数据源优先级 (provider 层)

| type | 实时行情 | 历史K线 |
| global | 长桥(可选)→ IBKR(可选)→ yfinance 批量 → 港股 akshare 兜底 (两源同开时 IBKR 结果覆盖长桥) | IBKR(可选)→ 长桥(可选)→ yfinance (港股可 `--akshare` 翻转) |
| cn | **akshare 优先** → yfinance (长桥可选前置, 沪深代码 .SH/.SZ) | **akshare 优先** → yfinance (长桥可选前置) |
| crypto | **Binance API** → Gate.io 现货 → Hyperliquid 永续(仅USD系) → yfinance (偏好 `crypto_source`/`--crypto-source`) | **Binance klines** → Gate.io → Hyperliquid → yfinance |
| index | — (指数无实时行情, 不参与持仓聚合) | 中国指数 **akshare 优先** → yfinance; 美股 SPX/NDX/DJI 有新浪兜底; 水泥网 (CEMPI 等) / 商品期货主力连续 (`IX.AU` 黄金 `IX.AG` 白银 `IX.JD` 鸡蛋 `IX.M` 豆粕 `IX.A` 豆一 `IX.B` 豆二 `IX.LH` 生猪 `IX.LC` 碳酸锂) / 恒生官网 (IX.HSAHP) 为专属源无兜底 |

代码搜索 (`tracker/search.py`) 为**纯在线分域接口**:
- A股/北交所: 东财 suggest (支持中文名/拼音)
- 美股/港股: IBKR 合约匹配 (Gateway 在线时) → 东财 suggest (纯数字补零/中文查询, 只收港股行,
  故 `search 腾讯`/`search 700` 可命中 0700.HK) → yfinance Search → 合法代码直查
- crypto: yfinance Search

无本地目录、无缓存快照; 接口失败即该域空结果, 由各域合法代码直查兜底。
美股/港股/crypto 搜中文名基本无结果 (东财 suggest 源在全球域只认领港股行)。

## 配置文件

数据目录 = 环境变量 `TRACKER_HOME` > 源码树仓库根 > 平台用户目录
(Linux `~/.local/share/tracker` / macOS `~/Library/Application Support/tracker` /
Windows `%APPDATA%\tracker`)。源码树里跑就是仓库根 (与旧版一致); pip 安装
(`pip install madeleine-tracker`) 后数据落平台目录, 不写进 site-packages。
运行产物 (行情缓存 quotes_cache.db / K线 HTML / 长桥登录状态) 在数据目录的
`var/` 子目录。首次使用 `tracker init` 把包内模板 `tracker/templates/*.example.json`
复制成数据目录下的真实配置 (已存在则跳过, `--force` 覆盖)。

- `portfolio.json`: `{"base_currency": "CNY", "holdings": [{"symbol", "type", "quantity", "avg_cost"}]}`
  `type` 为系统维护的内部域标记, 保存时自动按 `symbols.type_for_symbol()` 推导/覆写。
  均为用户本地数据 (已 gitignore), 模板随包分发在 `tracker/templates/portfolio.example.json`。
- `watchlist.json`: 扁平条目列表, 一个代码可属多个列表, 支持两级阈值与指标基准:
  ```json
  {"watchlist": [
    {"symbol": "TSLA", "lists": ["科技", "美股"], "upper_1": 420, "upper_2": 450,
     "lower_1": 280, "lower_2": 250, "note": "两级提醒", "type": "global"},
    {"symbol": "600036.SS", "lists": ["银行"], "metric": "pb", "upper_1": 1.2, "lower_1": 0.8},
    {"symbol": "AAPL", "lists": ["美股"], "metric": "mcap", "upper_1": 4000000000000, "note": "市值提醒"}
  ]}
  ```
  `metric` 阈值基准: `price`=现价 (默认, 阈值按当地货币) /
  `pb`=市净率 (阈值按 PB 倍数, 每股净资产来源: 条目手写 `pb` 键 > 行情自带
  长桥/akshare > Yahoo fundamentals, 缓存 6h) / `mcap`=总市值 (阈值按当地货币
  绝对金额, 来源同优先级: 条目手写 `mcap` 键 > 行情自带 > fundamentals)。
  省略 `metric` 时: 条目含手写 `pb` 键 → 基准推断为 pb; 含手写 `mcap` 键 → 推断为
  mcap; 否则 price (手写键既是数据覆盖值, 也会在缺省 metric 时暗示基准; 想保留
  覆盖键又用 price 基准需显式写 `metric: "price"`; 页面「指标基准」留空会钉为 price)。
  指标缺失 (crypto/无数据源) → 「指标无数据」, 不判区间内、不误报触发。
  `type` 由系统自动维护 (读取/保存时均会补写覆写), 手改无效。
- `settings.json`: 页面显示与数据源偏好 (已 gitignore, 模板 `tracker/templates/settings.example.json`):
  `color_scheme` (cn 红涨绿跌 / intl 绿涨红跌, 默认 cn) · `prefer_akshare` (默认 false) ·
  `use_ibkr` (默认 false) · `use_longport` (默认 false) ·
  `crypto_source` (auto/binance/gate/hyperliquid/yfinance, 默认 auto; 指定源前置, 失败回落链上其余源)。
  设置页切换数据源即时生效 (连进程内负缓存一起清); 直接改文件则重启 streamlit 生效。
- `ibkr.json`: IBKR 连接参数 + 交易所映射 (数据目录下)。**从配置文件读取, 不写死在代码里**：
  - 模板随包分发 `tracker/templates/ibkr.example.json`; `tracker init` 可生成数据目录下的
    真实配置 `ibkr.json` (可含敏感信息, 不会误提交; 源码树里该文件已 gitignore)
  - 优先级: 显式 path > env `IBKR_CONFIG=/path` > 数据目录 `ibkr.json` > 包内模板兜底
  - 缺字段自动继承模板/兜底值; 所有下划线开头的键 (`_comment`/`_说明`) 被忽略
  - Gateway 端口由 `mode` 决定 (paper=4002 / live=4001), 显式 `"port"` 优先, env `IBKR_MODE` 可临时覆盖
  - `exchanges` 键名与 `contract_spec` 读取的一致: US/HK/CN_SH/CN_STAR/CN_SZ/BJ/DE/GB/GB_IOB
    (LSE 国际挂盘 IOB, 默认 `IOB`, 对应 `.IL` 后缀)/CA/CA_V/CA_CN/CA_NE/AU/SG/BR;
    删除某个键即用内置默认路由
  - **开发 IBKR 相关功能前必读 `docs/ibkr-readonly-api.md`**: 本项目只用只读 API
    (行情/K线/汇率/持仓), 禁写 API (下单/撤单/订单信息查询, 只读模式下不可得)
- `longport.json`: 长桥 (LongPort) OpenAPI 接入 (`pip install longport` 后可用; env `LONGPORT_CONFIG` 可指定其他路径):
  - 模板随包分发 `tracker/templates/longport.example.json`; `tracker init` 生成数据目录下的真实配置
  - `auth: oauth` (推荐): 填入 OAuth 客户端注册返回的 `client_id` (注册命令见模板),
    然后 `tracker longport-login` 或 UI 导入页「长桥账户」标签登录; token 由 SDK
    缓存于 ~/.longport/openapi/tokens/<client_id> 并自动刷新
  - `auth: apikey`: 开发者中心 User Center 的 app_key/app_secret/access_token (90 天过期)
  - UI 登录引导在独立子进程运行 (SDK OAuth build 会冻结嵌入它的解释器), 状态文件
    <数据目录>/var/longport_oauth_state.json, 事件流: starting → waiting(含授权 URL) → ok/error
  - 长桥代码映射: 内部 0700.HK ↔ 700.HK, 600519.SS ↔ 600519.SH, AAPL ↔ AAPL.US,
    D05.SI ↔ D05.SG, 000001.SZ 等深市代码原样支持, 类别股/优先股 TAP-A ↔ TAP.A.US /
    WFC-PL ↔ WFC.PR.L.US; 德英加澳/北交所不支持 (行情前置自动跳过, 回落默认源)
  - 长桥账户里的退市/改码旧代码 (ERJ→EMBJ、BRFS→MBRF3.SA 等, 见 `tracker/symbol_migrations.py`)
    导入时自动迁移到接替代码, 无接替码则跳过并说明

## 常见 AI 操作流程

1. **查当前组合与提醒** — 直接问, 用快照:
   ```
   .venv/bin/python -m tracker.cli snapshot --json
   ```
   解读 `summary.total_value`(按 base_currency)、`triggered[]`(阈值触发) 等。

2. **查某只股票行情**:
   ```
   .venv/bin/python -m tracker.cli quote AAPL 0700.HK --json
   .venv/bin/python -m tracker.cli quote AAPL --longport --json   # 指定走长桥行情
   ```

3. **查汇率**:
   ```
   .venv/bin/python -m tracker.cli fx USD CNY EUR --json
   ```

4. **加自选 + 设阈值** (记住用 `--list` 指定归属列表, 阈值按基准指标解释; `--metric` 可选 price/pb/mcap):
   ```
   .venv/bin/python -m tracker.cli watchlist add NVDA --list 科技,美股 --upper1 260 --note 关注
   .venv/bin/python -m tracker.cli watchlist add 600036.SS --metric pb --upper1 1.2 --note 银行
   .venv/bin/python -m tracker.cli watchlist add AAPL --metric mcap --upper1 4000000000000 --note 市值
   ```
   已存在代码会合并列表/更新阈值, 不会重复。mcap 阈值按当地货币绝对金额;
   想离线固定口径可手写条目 `mcap` 键 (覆盖行情/数据源取值)。

5. **删自选**:
   ```
   .venv/bin/python -m tracker.cli watchlist remove NVDA
   ```

5b. **管理持仓** (增删改持仓, `--quantity` 必填, `--avg-cost` 按当地货币):
   ```
   .venv/bin/python -m tracker.cli portfolio list --json          # 查看持仓
   .venv/bin/python -m tracker.cli portfolio add AAPL --quantity 10 --avg-cost 180
   .venv/bin/python -m tracker.cli portfolio add 0700.HK --quantity 100 --avg-cost 330  # 港股代码自动归一
   .venv/bin/python -m tracker.cli portfolio remove NVDA
   .venv/bin/python -m tracker.cli portfolio set-base USD          # 修改基础货币
   ```
   已存在代码会更新数量/成本, 不会重复。

6. **搜索代码/名称** (不确定规范代码时先搜, 结果可直接喂 quote/kline/compare):
   ```
   .venv/bin/python -m tracker.cli search 腾讯 --json
   .venv/bin/python -m tracker.cli search 700 --limit 5        # 纯数字补零命中港股 0700.HK
   .venv/bin/python -m tracker.cli search maotai --exact       # 宽松输入→唯一最佳匹配
   ```

7. **导出监控阈值报告** (按列表分组: 现价/上下限/距阈值%/状态/触发汇总, 可写文件):
   ```
   .venv/bin/python -m tracker.cli report -f md            # 终端打印 markdown
   .venv/bin/python -m tracker.cli report -f json -o report.json   # 机器可读, 写文件
   .venv/bin/python -m tracker.cli report -f csv -w 科技    # 只看某列表, CSV(带BOM)
   ```
   `report` 参数: `-f md|csv|json`(默认 md) · `-w <列表>`(范围过滤) · `-o <文件>` · `--sort default|severity|change_desc|change_asc`。
   `--sort severity` 排序 (即触发项排前): 上限 II > 上限 I > 下限 II > 下限 I > 区间内 (深跌破重于浅跌破)。

8. **查历史涨跌**:
   ```
   .venv/bin/python -m tracker.cli history AAPL --months 12 --rows 5
   ```

8b. **看K线** (plotly 蜡烛图, 输出 <数据目录>/var/kline_<代码>.html (点号转下划线, 如 `var/kline_0700_HK.html`); K线有独立磁盘缓存 TTL 30 分钟):
   ```
   .venv/bin/python -m tracker.cli kline AAPL --months 12 --ma 5,20,60
   .venv/bin/python -m tracker.cli kline 600519.SS --period weekly --open   # 周K + 自动开浏览器
   ```
   支持 `--period daily|weekly|monthly`、`--no-volume`、`--refresh`、`--json`。页面侧 K线是独立页面: `streamlit run tracker/ui/app.py` 后侧边栏「K线」, 可查任意代码, 输入后才取数 (不预加载)。页面「范围」模式与指数K线/对比页范围下拉为 3/6/12/24/36/60 个月 (最长 5 年; 滑动模式本就一次加载上市以来全量历史); IBKR 源历史K线按 ≤365 天窗口分块拼接 (单次请求上限), 5 年区间同样可用。

8c. **多股对比 / 指数K线**:
   ```
   .venv/bin/python -m tracker.cli compare AAPL 0700.HK 600519.SS --json   # 归一化序列+区间涨跌
   # compare 与 kline 参数对齐, 均支持 --ibkr / --longport 前置源开关
   .venv/bin/python -m tracker.cli index-kline IX.DXY --months 12 --json   # 指数K线数据
   .venv/bin/python -m tracker.cli index-kline --list          # 全部已收录指数 (按分组)
   .venv/bin/python -m tracker.cli index-kline --list --json   # 机器可读目录 (code/name/group)
   ```
   指数代码规范 IX.<KEY>, 支持哪些指数用 `--list` 查 (含分组, 勿凭记忆硬编码清单)。

   页面侧「K线」页有三个子功能 tab: 单只查询 / 走势对比 (归一化折线, 任意代码) /
   加密货币对比 (仅 BASE-QUOTE 代码, 原始价格不归一化, 每币一个独立窗格蜡烛图,
   共享时间轴与十字光标)。加密货币对比仅 UI 提供, 无对应 CLI 子命令。

9. **IBKR / 长桥同步 / 导入持仓** (IBKR 需本机 Gateway 在线; 长桥需先 `longport-login`, 无凭据则报错回退):
   ```
   .venv/bin/python -m tracker.cli import ibkr --dry-run        # 预览 IBKR 账户持仓
   .venv/bin/python -m tracker.cli import ibkr --mode live      # 实盘追加导入
   .venv/bin/python -m tracker.cli longport-login               # 长桥 OAuth 登录 (浏览器授权)
   .venv/bin/python -m tracker.cli import longport --dry-run    # 预览长桥持仓 (港股/美股/沪深)
   .venv/bin/python -m tracker.cli watchlist import-longport --dry-run  # 预览长桥自选分组导入 (分组→列表, 代码归一)
   .venv/bin/python -m tracker.cli watchlist import-longport            # 追加合并到 watchlist.json (--overwrite 覆盖)
   .venv/bin/python -m tracker.cli import wallet eth 0x... --dry-run   # 链上余额预览 (eth/bsc/polygon/arbitrum/avalanche/tron/solana)
   .venv/bin/python -m tracker.cli import file 持仓.csv --dry-run      # A股券商 CSV/Excel 预览
   .venv/bin/python -m tracker.cli sync --dry-run               # 兼容入口, 等价 import ibkr (但 sync 缺省覆盖全部持仓!)
   ```
   `import` 子命令默认追加合并 (按代码更新数量/成本, 其余保留); `--overwrite` 覆盖全部;
   持仓导入任何写盘前自动备份 `.bak` (自选 `import-longport` 仅 `--overwrite` 时备份)。
   兼容入口 `sync` (→import ibkr) 相反: 缺省**覆盖**全部持仓, `--append` 才追加。
   Gateway 端口由 `ibkr.json` 的 `mode` 决定: paper=4002 / live=4001; 显式 `"port"` 优先; env `IBKR_MODE` 可临时覆盖。
   长桥行情: 各取数子命令加 `--longport` (如 `quote AAPL --longport`), 或 settings.json `"use_longport": true`。

10. **清缓存** (数据源限流/数据过期时):
    ```
    .venv/bin/python -m tracker.cli cache clear
    ```

## 注意事项

- 非美股行情普遍延迟 15-30 分钟, 免费数据源无 SLA; 加密货币 Binance 为实时数据。
- 行情/汇率失败时会打印 `⚠ 问题` 或 JSON 里 `issues`/`missing`, 属正常降级, 不是 bug。
- A股默认走 akshare (无需 `--akshare`); `--akshare` 开关现在仅影响港股数据源顺序。
- 长桥 (LongPort): `pip install longport` 为可选依赖; 未安装/未配置时 `--longport` 与 `import longport`
  报可读错误并回退默认数据源。UI 登录引导页在「导入」页长桥标签, 状态经 var/longport_oauth_state.json。
  授权子进程会独占 longport.json 的 callback_port (默认 60355), 同一端口只能跑一个登录流程。
- Binance 对部分数据中心 IP 有限流; 失败自动降级 Gate.io → Hyperliquid → yfinance。
  搜索全部为在线接口; 中文查询仅东财 suggest 支持 (A股/北交所全量, 美股/港股域只认领港股行),
  美股/港股/crypto 直接搜中文名基本无结果。
- 取数失败的代码进进程内负缓存 (5 分钟 TTL 内跳过重试), 报「近期全部数据源失败 (负缓存)」属预期行为。
- 修改 `portfolio.json`/`watchlist.json` 后如需页面生效, 让用户在页面点「重载」或重启 streamlit。
- 单元测试: `.venv/bin/python -m pytest tests/ -q` (纯逻辑, 不联网)。
