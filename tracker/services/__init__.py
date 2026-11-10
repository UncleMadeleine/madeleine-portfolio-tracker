"""独立用例层: 用例 = 取数 (providers/fx) + 纯函数计算 + 组装视图.

本层默认不做存储 I/O: 入参是调用方 (cli / ui) 已加载的 dict/list,
返回 DataFrame/dict 视图, 供终端渲染、JSON 输出与 Streamlit 页面共用。

- rules.py     兼容再导出 (纯规则已提升为顶层 tracker.rules)
- watchlist.py 自选视图组装 + 取数用例 (watchlist list / report 复用)
- snapshot.py  快照用例 (take_snapshot / snapshot_json)
- warm.py      缓存保温/预热服务 (存储 I/O 例外: 从磁盘推导范围并回写
               SQLite; 无 Streamlit 依赖, UI 与 CLI `tracker warm` 共用)
"""
