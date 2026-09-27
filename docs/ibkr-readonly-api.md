# IBKR TWS/IB Gateway API 只读 (Read-Only API) 能力调研

> 日期：2026-09-27
> 背景：本项目通过 `ib_async/ib_insync` 连接本机 IB Gateway 做**只读**取数
> （行情 / K线 / 汇率 / 账户持仓）。本文档基于 IBKR 官方文档（TWS API 文档 +
> TWS API Reference 的 protobuf 消息清单）梳理 TWS API 的读/写分类，以及
> **Gateway 处于只读模式时哪些接口仍可调用**，供后续开发时避免踩坑。

---

## 一、结论摘要

| 维度 | 结论 |
|------|------|
| **只读由谁控制** | 由 **TWS / IB Gateway 的全局配置**「API → Settings → Read-Only API」（对应协议字段 `readOnlyApi`）控制，在服务端生效 |
| **`ib_async.connect(readonly=True)` 是什么** | 只是**客户端库自身行为**：跳过连接时自动拉取 open orders / completed orders。**不改变、也不代表 Gateway 的只读设置**，没有任何权限含义 |
| **只读下可用的** | 全部数据请求类 API：行情（实时/历史/k线/tick-by-tick/深度）、合约与代码搜索、账户与组合（持仓、账户摘要、账户更新、PnL）、扫描器、新闻、显示组查询、会话管理 |
| **只读下被拒/拿不到的** | ① 一切写操作：下单/改单/撤单/全球撤单/期权行权/FA 分配替换/更新配置/更新显示组/账户 Verify；② **订单信息类查询**（open orders / auto open orders / all open orders / executions / completed orders）——官方明确"Information about orders is not available to the API when read-only mode is enabled" |
| **对本项目的含义** | 现有功能（价格/K线/汇率/持仓导入）全部落在只读可达集合内；**不要**新增下单、订单查询、持仓导出以外的写操作；若未来要做订单/成交展示，必须做降级提示（用户 Gateway 只读时数据不可得） |

官方定义（IBKR Campus → TWS API Reference → `ApiSettingsConfig`）：

| 字段 | 说明 |
|------|------|
| `readOnlyApi` (bool) | "true indicates **modifications cannot be made through the API**" |

官方定义（TWS API 文档 → Initial Setup → Read Only API）：

> The API Settings dialogue allows you to configure TWS to not accept API orders with
> the "Read Only" setting. By default, "Read Only" is enabled as an additional
> precautionary measure. **Information about orders is not available to the API when
> read-only mode is enabled.**

即：只读模式 = 服务端拒绝一切"修改"，且**订单域信息整体不可见**（不仅是不能下，也不能查）。

---

## 二、读 / 写 API 分类总表

分类依据：IBKR Campus TWS API protobuf Reference 列出的全部客户端请求消息
（= `IBApi.EClient`（Python: `ibapi.client.EClient`）的出站请求函数），
结合只读语义（"modifications cannot be made through the API" + "order information
is not available"）判定。

### 2.1 写 API（只读模式下被拒绝 / 不可用）

| API 函数 | protobuf 消息 | 说明 |
|----------|--------------|------|
| `placeOrder` | `PlaceOrderRequest` | 下单、改单（含 Transmit 未发送单） |
| `cancelOrder` | `CancelOrderRequest` | 撤单 |
| `reqGlobalCancel` | `GlobalCancelRequest` | 取消全部订单 |
| `exerciseOptions` | `ExerciseOptionsRequest` | 期权行权 |
| `replaceFA` | `ReplaceFA` / `FaReplace` | FA 顾问账户分配方法替换 |
| `updateConfig` | `UpdateConfigRequest` | 更新 TWS/Gateway 配置（**含 API 自身设置，可自己去改只读开关**） |
| `updateDisplayGroup` | `UpdateDisplayGroupRequest` | 修改 TWS 显示组（TWS 侧配置状态变更） |
| `verifyRequest` / `verifyMessage` / `verifyCompleted` | `VerifyRequest` / `VerifyMessageRequest` / `VerifyCompleted` | 账户级确认回写（曾用于数字货币迁移等），会影响 IB 账户数据，按写对待 |

### 2.2 订单信息查询（只读模式下拿不到数据）

官方明令"order information is not available"，下列查询在只读时返回空/报错，
**不要**把它们当核心功能依赖：

| API 函数 | protobuf 消息 |
|----------|--------------|
| `reqOpenOrders` | `OpenOrdersRequest` |
| `reqAllOpenOrders` | `AllOpenOrdersRequest` |
| `reqAutoOpenOrders` | `AutoOpenOrdersRequest` |
| `reqExecutions` | `ExecutionRequest` |
| `reqCompletedOrders` | `CompletedOrdersRequest` |

（`reqIds` / `nextValidId` 属于会话握手信息，仍会返回，但本项目不必关心。）

### 2.3 只读模式下可调用（读 API，白名单）

| 域 | API 函数 | protobuf 消息 | 本项目用途 |
|----|----------|--------------|-----------|
| **会话/连接** | `startApi`、`setServerLogLevel`、`reqCurrentTime`、`reqCurrentTimeInMillis`、`reqManagedAccts`、`reqUserInfo` | `StartApiRequest`、`SetServerLogLevelRequest`、`CurrentTimeRequest`、`ManagedAccountsRequest`、`UserInfoRequest` | 建连必需 |
| **账户/组合** | `reqPositions`、`cancelPositions`、`reqPositionsMulti`、`cancelPositionsMulti` | `PositionsRequest`、`PositionsMultiRequest` | **持仓导入**（`fetch_positions`） |
| | `reqAccountSummary`、`cancelAccountSummary` | `AccountSummaryRequest` | 账户净值/现金 |
| | `reqAccountUpdates`、`reqAccountUpdatesMulti`、`cancelAccountUpdatesMulti` | `AccountDataRequest`、`AccountUpdatesMultiRequest` | 组合订阅更新 |
| | `reqPnL`、`reqPnLSingle` 及 cancel | `PnLRequest`、`PnLSingleRequest` | 盈亏 |
| | `reqFamilyCodes`、`reqSoftDollarTiers` | `FamilyCodesRequest`、`SoftDollarTiersRequest` | 只读元信息 |
| **合约/搜索** | `reqContractDetails`、`reqMatchingSymbols`、`reqSecDefOptParams` | `ContractDataRequest`、`MatchingSymbolsRequest`、`SecDefOptParamsRequest` | **代码搜索**（`search_matches`）、`qualifyContracts` |
| | `reqCalculateImpliedVolatility`、`reqCalcOptionPrice` 及 cancel | `CalculateImpliedVolatilityRequest`、`CalculateOptionPriceRequest` | 期权计算（只读） |
| **实时行情** | `reqMktData`/`cancelMktData`、`reqMktDepth`/`cancelMktDepth`、`reqMktDepthExchanges`、`reqRealTimeBars`/`cancelRealTimeBars`、`reqTickByTickData`/`cancelTickByTickData`、`reqMarketDataType`、`reqSmartComponents`、`reqMarketRule`、`rerouteCFD`（marketData/depth） | `MarketDataRequest`、`MarketDepthRequest`、`RealTimeBarsRequest`、`TickByTickRequest`、`MarketDataTypeRequest`、`SmartComponentsRequest`、`MarketRuleRequest`、`RerouteMarketDataRequest`/`RerouteMarketDepthRequest` | **报价**（`reqTickers`）、延迟数据类型 |
| **历史行情** | `reqHistoricalData`/`cancelHistoricalData`、`reqHeadTimeStamp`/`cancelHeadTimeStamp`、`reqHistogramData`/`cancelHistogramData`、`reqHistoricalTicks`/`cancelHistoricalTicks`、`reqHistoricalNews`/`cancelHistoricalNews` | `HistoricalDataRequest`、`HeadTimestampRequest`、`HistogramDataRequest`、`HistoricalTicksRequest`、`HistoricalNewsRequest` | **K线**（`reqHistoricalData`） |
| **扫描器** | `reqScannerParameters`、`reqScannerSubscription`/`cancelScannerSubscription` | `ScannerParametersRequest`、`ScannerSubscriptionRequest` | 市场扫描（只读） |
| **新闻/公告** | `reqNewsProviders`、`reqNewsBulletins`/`cancelNewsBulletins`、`reqNewsArticle`、`reqContractNews` | `NewsProvidersRequest`、`NewsBulletinsRequest`、`NewsArticleRequest` | 资讯 |
| **Wall Street Horizon** | `reqWshMetaData`、`reqWshEventData` 及 cancel | `WshMetaDataRequest`、`WshEventDataRequest` | 事件数据（只读） |
| **显示组** | `queryDisplayGroups`、`subscribeToGroupEvents`、`unsubscribeFromGroupEvents` | `QueryDisplayGroupsRequest`、`SubscribeToGroupEventsRequest`、`UnsubscribeFromGroupEventsRequest` | 只读查询/退订自身订阅 |
| **FA（读）** | `requestFA`（查询 groups/profiles） | `FaRequest` | 顾问配置查询 |

> 注：`cancel*` 系列只是取消自己的数据订阅，不构成账户修改，只读下可用。
> `updateDisplayGroup` 会改 TWS 侧显示组状态，归为写；`unsubscribeFromGroupEvents`
> 只退订自己的事件，归为读。

---

## 三、开发注意事项（给 agent 的红线）

1. **只读能力是用户 Gateway 的服务端设置，不是代码里的 `readonly=True`。**
   - `tracker/ibkr.py` 的 `ib.connect(..., readonly=True, fetchFields=StartupFetchNONE)`
     仅表示：客户端库不自动拉订单/成交；`StartupFetchNONE` 连持仓/账户更新也不自动拉，
     需要时显式 `reqPositions`。二者都不赋予也不限制任何权限。
   - 真正决定能不能写的是 Gateway 的 **Read-Only API**（`readOnlyApi`）默认勾选状态。
2. **禁止引入任何写 API**：`placeOrder` / `cancelOrder` / `reqGlobalCancel` /
   `exerciseOptions` / `replaceFA` / `updateConfig` / `updateDisplayGroup` /
   `verify*`。就算 Gateway 某天被用户取消只读，本项目定位是只读取数，
   不得顺手实现交易能力（安全边界，也是仓库"本地优先、零凭据写入"的定位）。
3. **订单/成交域功能一律不可用**：`reqOpenOrders` / `reqAllOpenOrders` /
   `reqAutoOpenOrders` / `reqExecutions` / `reqCompletedOrders` 在只读模式下
   官方明确不可得。若产品要求展示订单/成交，必须：
   - 明确降级提示（"当前 Gateway 为只读模式，订单信息不可用"）；
   - 不要静默返回空列表造成"没有订单"的误读。
4. **已有功能全部安全**：价格（`reqTickers`/`reqMktData`）、K线
   （`reqHistoricalData`）、汇率（CASH 合约 `reqTickers`）、搜索
   （`reqMatchingSymbols`）、持仓（`reqPositions`）都在只读白名单内。
5. **市场数据类型**：`ibkr.json` 的 `market_data_type=3` = 延迟行情（1 live /
   2 frozen / 3 delayed / 4 delayed-frozen）。延迟数据属于只读行情范畴，
   与 Read-Only API 正交，不影响权限判断。
6. **排查手段**：若某功能"在只读下拿不到数据"，先用 TWS/Gateway 的
   API 消息日志（Configure → Settings → API → 勾选 "Create API message log"）
   看请求是否发出、是否被拒；代码里所有调用都要 try/except 降级到下一数据源
   （本项目现有模式：IBKR → yfinance/akshare）。
7. **加拿大产品 API 交易限制**（官方 Limitations 页）：Interactive Brokers
   Canada 账户不允许通过 API 提交加拿大交易所订单——只读取数不受影响，
   但再次印证本项目不做交易的方向。

---

## 四、参考（官方来源）

- TWS API 文档 → Initial Setup → "Read Only API"：
  https://interactivebrokers.github.io/tws-api/initial_setup.html
  （原文："...configure TWS to note accept API orders with the 'Read Only' setting...
  Information about orders is not available to the API when read-only mode is enabled."）
- IBKR Campus → TWS API Documentation（现行版文档入口）：
  https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/
- IBKR Campus → TWS API Reference → `ApiSettingsConfig`（`readOnlyApi` 字段定义）：
  https://ibkrcampus.com/docs/tws-api/protobuf/api-settings-config.md
- IBKR Campus → TWS API Reference → protobuf 消息全清单（本文分类依据）：
  https://ibkrcampus.com/docs/tws-api/llms.txt
- IBKR Campus → TWS API Limitations：
  https://ibkrcampus.com/docs/tws-api/doc/notes-limitations/tws-api-limitations.md
