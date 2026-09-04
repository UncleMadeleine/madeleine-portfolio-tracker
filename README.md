# OpenBB Portfolio Tracker

本地多市场投资组合追踪：**A股 / 港股 / 美股 / 德股 / 英股 / 加股 / 澳股**，基于 OpenBB (yfinance) + akshare 双数据源，Streamlit 页面展示，全部免费、无 API Key。

功能：**多币种持仓追踪 + 自选股 (Watchlist) 价格阈值提醒 + IBKR 行情接入**。

## 快速开始

```bash
cd ~/Project/openbb-portfolio-tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动页面 (http://localhost:8501)
.venv/bin/streamlit run app.py

# 或命令行快照 (持仓 + 自选 + 阈值提醒)
.venv/bin/python -m tracker.snapshot
.venv/bin/python -m tracker.snapshot --base USD   # 切换基础货币
.venv/bin/python -m tracker.snapshot --ibkr       # 优先走 IBKR

# 从 IBKR 账户同步真实持仓 (需 TWS/IB Gateway 已登录)
.venv/bin/python -m tracker.ibkr_sync
.venv/bin/python -m tracker.ibkr_sync --dry-run   # 仅预览不写入

# 单元测试 (不联网)
.venv/bin/python -m pytest tests/ -q
```

## 代码规范 (Yahoo Finance 后缀)

| 市场 | 后缀 | 示例 | 币种 | 说明 |
|---|---|---|---|---|
| 美股 | 无 | `AAPL` | USD | |
| A股 | `.SS` / `.SZ` | `600519.SS` `000001.SZ` | CNY | 也接受 `.SH` |
| 港股 | `.HK` | `0700.HK` `0941.HK` | HKD | **4 位补零**；`00700.HK` 会自动归一 |
| 德股 | `.DE` 等 | `SAP.DE` | EUR | `.F/.BE/.DU/.HM/.SG/.MU` 均可 |
| 英股 | `.L` | `BP.L` | GBP | Yahoo 报价单位是便士(GBp)，系统自动 ÷100 换算为英镑 |
| 加股 | `.TO` 等 | `RY.TO` | CAD | `.V` (TSXV) `.CN` `.NE` 均可 |
| 澳股 | `.AX` | `BHP.AX` | AUD | |

`avg_cost`（成本）按**当地货币**填写；英股填**英镑**（如 4.30 = £4.30，不是便士）。

## 自选股与价格阈值提醒

`watchlist.json` 配置自选股，每条可设 `upper`（上限）/ `lower`（下限）与备注，均可只设一侧或全不设：

```json
{
  "watchlist": [
    { "symbol": "TSLA", "upper": 450, "lower": 250, "note": "突破追 / 回调买" },
    { "symbol": "600036.SS", "upper": 55, "lower": 38, "note": "招商银行" },
    { "symbol": "DBK.DE", "lower": 18, "note": "德银 回调关注" },
    { "symbol": "BRK-B", "note": "无阈值纯观察" }
  ]
}
```

- **阈值按当地货币**（与显示的现价同币种），到达或越过（含等于）即触发
- 状态：`🔴 高于上限` / `🟢 低于下限` / `⚪ 区间内`；触发项排在最前
- `距上限%` / `距下限%` = 还需变动百分之几才触发（负值 = 已越过）
- 页面「自选观察」tab 顶部汇总提醒，tab 标签带 🔔 徽标；CLI 快照同步输出 🔔 阈值提醒
- 持仓与自选**共用一次批量行情请求**，多加自选不增加请求次数

## 架构

```
app.py                  Streamlit 页面 (持仓编辑/指标/配置/走势/自选提醒)
tracker/
├── symbols.py          代码解析、市场识别、GBp/港股补零归一
├── prices.py           行情路由: IBKR 优先 (批量快照) → yfinance 批量 → akshare 降级 → 逐个重试
├── fx.py               汇率: CFETS(akshare) 优先, yfinance 货币对兜底(直对/逆对/USD桥)
├── analytics.py        组合视图与指标 (纯函数)
├── watchlist.py        自选股视图与价格阈值状态 (纯函数 + 配置读写)
├── ibkr.py             IBKR 行情接入 + 持仓同步 (可选依赖 ib_async, 失败静默回退)
├── ibkr_sync.py        CLI: 从 IBKR 账户持仓生成 portfolio.json
└── snapshot.py         CLI 快照 (持仓 + 自选, 支持 --ibkr)
tests/                  纯逻辑单元测试 (mock 数据源, 不联网)
portfolio.json          持仓配置 (页面可直接编辑保存)
watchlist.json          自选股配置 (页面可直接编辑保存)
ibkr.json               IBKR 连接配置 + 交易所映射 (已含默认值)
```

### 数据源与降级策略

行情路由优先级（可叠加启用）：

| 优先级 | 数据源 | 说明 |
|---|---|---|
| 1 | **IBKR** (TWS/IB Gateway) | 有订阅则为实时；无订阅则 delayed；一次 `reqTickers` 批量快照 |
| 2 | **OpenBB → yfinance** | 批量 quote（1 次请求）+ 失败逐个重试 |
| 3 | **akshare** (东财) | A股/港股 spot + 历史；大陆网络下可优先切 akshare |
| 4 | **CFETS / yfinance** | 汇率：CFETS 一次拿全 XXX/CNY；直对/逆对/USD 桥接兜底 |

- 页面侧栏可勾选「A股/港股优先 akshare」「🔗 IBKR 行情」
- 英股 GBp 便士报价自动换算为 GBP；汇率缓存 10 分钟，行情缓存 5 分钟
- IBKR 连接参数见 `ibkr.json`（默认 `127.0.0.1:7497` 模拟盘）；**交易所映射也在同一文件**，可按需覆盖（A股默认 `SEHK`，因沪深港通合约挂在 HKEX 下，需配 `tradingClass`）
- IBKR 断开时自动静默回退下一级数据源，不影响页面运行；持仓同步见下方

### 从 IBKR 同步真实持仓

```bash
.venv/bin/python -m tracker.ibkr_sync --dry-run   # 预览
.venv/bin/python -m tracker.ibkr_sync              # 写入 portfolio.json (自动备份 .bak)
```

- 自动将 IBKR 账户股票持仓转换为 Yahoo 代码并写入 `portfolio.json`
- 保留原有 `base_currency`；`avg_cost` 取自 IBKR（合约货币每股均价，含佣金）
- 无法映射为 Yahoo 代码的标的（权证/期权/基金等）会跳过并在控制台提示
- 反向映射规则：`SEHK + CNY → .SS/.SZ`；`SEHK + HKD → .HK`；`IBIS/FWB + EUR → .DE`；`LSE + GBP → .L`；`TSE + CAD → .TO`；`ASX + AUD → .AX`；`SMART + USD → 原码`

## 已知限制

- 免费数据源：非美股行情普遍延迟 15-30 分钟；无 SLA，偶发限流（已内置批量请求 + 重试缓解）
- akshare 东财 spot 接口对部分数据中心 IP 不友好（本项目所在机器即如此），此时自动回落 Yahoo
- CFETS 汇率为中间价，与离岸 CNH 有细微差异，组合展示场景可忽略
- `今日估算` 按各持仓 `涨跌幅 × 当前市值` 近似，非精确日内盯市
- IBKR 行情需本机运行 TWS/IB Gateway 且 API 已启用；A股/港股数据若无市场数据订阅，`reqTickers` 可能返回空值，自动回退 Yahoo/akshare
- IBKR A股合约默认映射为 `SEHK/CNY`，若你的账户显示不同交易所代码，在 `ibkr.json` 中修改 `exchanges.CN`

## Roadmap

- [x] ~~IBKR 行情接入 + 持仓同步~~ (已完成)
- [ ] 交易流水记录与分批成本 (FIFO)
- [ ] 分红/拆分事件跟踪
- [ ] EODHD 付费 provider 接入 (升级数据质量)
- [ ] 历史净值曲线 (按日聚合组合市值)
