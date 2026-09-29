"""退市/改码代码 → 接替代码迁移表.

数据源侧 (yfinance/长桥) 对退市代码只会报错或返回停更死价; 每次长桥
重新导入自选时, 账户侧仍留着这些旧代码, append 合并会把死码带回来。
本表集中维护「已核实」的迁移关系 (公告日期 + 接替代码), 供:
  - 长桥自选导入 (watchlist_to_rows): 旧码自动改写为接替码
  - watchlist.json 清理工具: 一次性迁移已有条目

RETIRED (无接替码): 该证券已清算/退市且无跟踪等价物, 导入时跳过。
表为手工核实 (交易所公告/新闻), 不做自动探测 —— 宁缺勿错。
"""

from __future__ import annotations

# old yahoo 代码 → (新 yahoo 代码或 None, 说明)
SYMBOL_MIGRATIONS: dict[str, tuple[str | None, str]] = {
    # Embraer: 2025-11-03 NYSE 改码 (ERJ → EMBJ)
    "ERJ": ("EMBJ", "Embraer 2025-11-03 改码"),
    # 趣店: 2025-12-22 更名 High Templar Tech 改码 (QD → HTT)
    "QD": ("HTT", "Qudian 更名 High Templar Tech 2025-12-22 改码"),
    # Eletrobras → Axia Energia: 2025-11-10 改名, 优先股 ADR 转 OTC
    "EBR-B": ("AXIAY", "Eletrobras 更名 Axia Energia 2025-11-10, 优先股 ADR 转 OTC"),
    # EBR-B 与 AXIA-P 同一公司同一资产: 两者取 AXIAY, 另一个去重
    "AXIA-P": (None, "Axia Energia 优先股 ADR (与 EBR-B 同一资产, 已由 AXIY 承接)"),
    # BRF 2025-09 并入 Marfrig (MBRF), NYSE ADR 退市, B3 主上市
    "BRFS": ("MBRF3.SA", "BRF 并入 MBRF 2025-09, NYSE ADR 退市, 转 B3"),
    # GPA (Companhia Brasileira de Distribuição) ADR 退市
    "CBDBY": (None, "GPA ADR 退市 (B3: PCAR3.SA 未跟进)"),
    # Direxion Daily Cloud Computing Bull 2X: 2025-07 清盘
    "CLDL": (None, "Direxion 云计算 2X ETF 2025-07 清盘"),
    # SMI Vantage: SGX 2025-05-15 退市
    "Y45.SI": (None, "SMI Vantage SGX 2025-05-15 退市"),
}


def migrate_symbol(yahoo: str) -> tuple[str, str | None]:
    """旧代码 → (迁移后代码, 说明).

    返回:
        (yahoo, None)  非退市代码, 原样通过
        (new, reason)  已迁移到接替代码
        (yahoo, reason) 已退市且无接替码 —— 调用方应跳过并提示
    """
    hit = SYMBOL_MIGRATIONS.get(str(yahoo).strip().upper())
    if hit is None:
        return yahoo, None
    new, reason = hit
    if new is None:
        return yahoo, reason
    return new, reason
