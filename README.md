# OpenBB Portfolio Tracker

本地多市场投资组合追踪：**A股 / 港股 / 美股 / 德股 / 英股 / 加股 / 澳股**，基于 OpenBB (yfinance) + akshare 双数据源，Streamlit 页面展示，全部免费、无 API Key。

## 快速开始

```bash
cd ~/Project/openbb-portfolio-tracker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动页面 (http://localhost:8501)
.venv/bin/streamlit run app.py

# 或命令行快照
.venv/bin/python -m tracker.snapshot
.venv/bin/python -m tracker.snapshot --base USD   # 切换基础货币

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

## 架构

```
app.py                  Streamlit 页面 (持仓编辑/指标/配置/走势)
tracker/
├── symbols.py          代码解析、市场识别、GBp/港股补零归一
├── prices.py           行情路由: 批量 yfinance 为主, A股/港股 akshare 降级
├── fx.py               汇率: CFETS(akshare) 优先, yfinance 货币对兜底(直对/逆对/USD桥)
├── analytics.py        组合视图与指标 (纯函数)
└── snapshot.py         CLI 快照
tests/                  纯逻辑单元测试 (mock 数据源, 不联网)
portfolio.json          持仓配置 (页面可直接编辑保存)
```

### 数据源与降级策略

| 数据 | 主路径 | 兜底 |
|---|---|---|
| 行情 (7 市场) | OpenBB → yfinance **批量 quote** (1 次请求) | 失败代码逐个重试；A股/港股可切 akshare (东财 spot) |
| 历史 K 线 | OpenBB → yfinance | A股/港股 → akshare 东财历史 |
| 汇率 | CFETS `fx_spot_quote` (1 次请求含全部 XXX/CNY，交叉可得任意对) | yfinance 货币对：直对 → 逆对 → USD 桥接 |

- 页面侧栏可勾选「A股/港股优先 akshare」——大陆网络 Yahoo 不稳时使用
- 英股 GBp 便士报价自动换算为 GBP；汇率缓存 10 分钟，行情缓存 5 分钟

## 已知限制

- 免费数据源：非美股行情普遍延迟 15-30 分钟；无 SLA，偶发限流（已内置批量请求 + 重试缓解）
- akshare 东财 spot 接口对部分数据中心 IP 不友好（本项目所在机器即如此），此时自动回落 Yahoo
- CFETS 汇率为中间价，与离岸 CNH 有细微差异，组合展示场景可忽略
- `今日估算` 按各持仓 `涨跌幅 × 当前市值` 近似，非精确日内盯市

## Roadmap

- [ ] 交易流水记录与分批成本 (FIFO)
- [ ] 分红/拆分事件跟踪
- [ ] EODHD 付费 provider 接入 (升级数据质量)
- [ ] 历史净值曲线 (按日聚合组合市值)
