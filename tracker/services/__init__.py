"""独立用例层: 用例 = 取数 (providers/fx) + 纯函数计算 + 组装视图.

本层不做存储 I/O: 入参是调用方 (cli / ui) 已加载的 dict/list,
返回 DataFrame/dict 视图, 供终端渲染、JSON 输出与 Streamlit 页面共用。

- rules.py     阈值规则 (纯函数: 两级上下限状态判定 + 距离)
- watchlist.py 自选视图组装 + 取数用例 (watchlist list / report 复用)
- snapshot.py  快照用例 (take_snapshot / snapshot_json)
"""
