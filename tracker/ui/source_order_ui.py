"""设置页的数据源顺序编辑器: 拖动排序 + 即改即存.

自定义组件源码在 ``tracker/ui/components/source_order/index.html``:
原生 HTML5 拖拽, 不引入第三方前端依赖; 同时提供 ↑/↓ 按钮与
Alt/Ctrl + ↑/↓ 键盘操作, 触屏/无鼠标环境也能调整。
"""

from __future__ import annotations

import copy
import threading
from functools import partial
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from tracker import source_order as so
from tracker import storage

_COMPONENT_DIR = Path(__file__).with_name("components") / "source_order"
_source_order_component = None
_component_lock = threading.Lock()


def _component():
    """惰性注册自定义组件 (导入模块时不触发 Streamlit 运行时警告)."""
    global _source_order_component
    if _source_order_component is None:
        with _component_lock:
            if _source_order_component is None:
                _source_order_component = components.declare_component(
                    "source_order", path=str(_COMPONENT_DIR)
                )
    return _source_order_component


def _eodhd_modes(eodhd_cfg: dict) -> tuple[str, str]:
    """默认链展示用的 EODHD 角色 (off 视为 fallback, 不影响其余源位置)."""
    quotes = str(eodhd_cfg.get("quotes") or "fallback")
    if quotes not in ("primary", "fallback"):
        quotes = "fallback"
    history = str(eodhd_cfg.get("priority") or "primary")
    if history not in ("primary", "fallback"):
        history = "primary"
    return quotes, history


def _display_ids(
    domain: str, channel: str, settings: dict, eodhd_cfg: dict
) -> list[str]:
    quotes_mode, history_mode = _eodhd_modes(eodhd_cfg)
    return so.display_order(
        domain,
        channel,
        prefer_akshare=bool(settings.get("prefer_akshare")),
        crypto_source=str(settings.get("crypto_source") or "auto"),
        eodhd_quotes=quotes_mode,
        eodhd_priority=history_mode,
        akshare_supported=True,
    )


def _item_meta(
    source_id: str,
    domain: str,
    channel: str,
    eodhd_cfg: dict,
    eodhd_ok: bool,
) -> dict:
    meta = so.SOURCE_META.get(source_id)
    item = {
        "id": source_id,
        "label": meta.label if meta else source_id,
        "note": meta.note if meta else "",
        "badge": "",
    }
    if source_id == "eodhd":
        if not eodhd_ok:
            item["badge"] = "未启用"
        elif channel == "quote" and str(eodhd_cfg.get("quotes")) == "off":
            item["badge"] = "quotes=off"
    elif source_id == "yfinance" and domain == "global":
        item["note"] = "免 Key；全球域有批量请求"
    elif source_id == "akshare" and domain == "index":
        item["note"] = "仅中国指数 / 美股 SPX·NDX·DJI 有 akshare 源"
    return item


def _items(
    domain: str, channel: str, eodhd_cfg: dict, eodhd_ok: bool
) -> list[dict]:
    return [
        _item_meta(source_id, domain, channel, eodhd_cfg, eodhd_ok)
        for source_id in so.CATALOG[domain][channel]
    ]


def _widget_key(domain: str, channel: str) -> str:
    return f"set_source_order_{domain}_{channel}"


def _legacy_default(domain: str, channel: str, settings: dict) -> list[str]:
    """该通道当前的默认链 (含旧版快捷偏好的影响)."""
    from tracker import eodhd as eodhd_mod

    cfg = eodhd_mod.load_config()
    quotes = str(cfg.get("quotes") or "fallback")
    if quotes not in ("primary", "fallback"):
        quotes = "fallback"
    history = str(cfg.get("priority") or "primary")
    if history not in ("primary", "fallback"):
        history = "primary"
    return so.default_order(
        domain,
        channel,
        prefer_akshare=bool(settings.get("prefer_akshare")),
        crypto_source=str(settings.get("crypto_source") or "auto"),
        eodhd_quotes=quotes,
        eodhd_priority=history,
    )


def _store_order(domain: str, channel: str, order: list[str]) -> None:
    """把一条源链写进 settings.json; 只复位该域相关的历史偏好键."""
    settings = storage.load_settings(storage.SETTINGS_PATH)
    raw = settings.get("source_order")
    raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    domain_cfg = raw.get(domain)
    if not isinstance(domain_cfg, dict):
        domain_cfg = {}
    domain_cfg[channel] = list(order)

    payload: dict = {}
    # 旧版快捷开关 (prefer_akshare / crypto_source) 影响同一域的行情与历史
    # 两条链。保存其中一条时, 若另一条还没显式配置, 先把「当前生效的默认链」
    # 固化下来, 再复位开关 —— 否则编辑行情会把未编辑的历史链悄悄改回默认。
    if domain == "global":
        if settings.get("prefer_akshare"):
            other = "history" if channel == "quote" else "quote"
            if so.configured("global", other) is None:
                domain_cfg[other] = _legacy_default("global", other, settings)
        payload["prefer_akshare"] = False
    elif domain == "crypto":
        if str(settings.get("crypto_source") or "auto") != "auto":
            other = "history" if channel == "quote" else "quote"
            if so.configured("crypto", other) is None:
                domain_cfg[other] = _legacy_default("crypto", other, settings)
        payload["crypto_source"] = "auto"

    raw[domain] = domain_cfg
    payload["source_order"] = raw
    storage.save_settings(payload, storage.SETTINGS_PATH)
    so.clear_cache()
    _clear_source_caches()


def _reset_order(domain: str, channel: str) -> None:
    settings = storage.load_settings(storage.SETTINGS_PATH)
    raw = settings.get("source_order")
    if not isinstance(raw, dict):
        return
    raw = copy.deepcopy(raw)
    domain_cfg = raw.get(domain)
    if not isinstance(domain_cfg, dict) or channel not in domain_cfg:
        return
    domain_cfg.pop(channel, None)
    if domain_cfg:
        raw[domain] = domain_cfg
    else:
        raw.pop(domain, None)
    storage.save_settings({"source_order": raw}, storage.SETTINGS_PATH)
    so.clear_cache()
    _clear_source_caches()
    st.toast("已恢复默认数据源顺序")


def _on_order_change(domain: str, channel: str, key: str) -> None:
    value = st.session_state.get(key)
    order = so.normalize(domain, channel, value)
    if order is None:
        st.error("数据源顺序无效: 至少保留一个数据源。")
        return
    try:
        _store_order(domain, channel, order)
    except Exception as e:  # noqa: BLE001 - 设置保存失败不应炸整页
        st.error(f"数据源顺序保存失败: {e}")
        return
    st.toast(f"{so.DOMAIN_META[domain][0]} · {so.CHANNEL_LABELS[channel]} 顺序已保存")


def _clear_source_caches() -> None:
    """延迟导入避免 settings_page ↔ source_order_ui 的循环导入."""
    from .settings_page import _clear_source_caches as clear_caches

    clear_caches()


def _render_one(
    domain: str,
    channel: str,
    settings: dict,
    eodhd_cfg: dict,
    eodhd_ok: bool,
) -> None:
    key = _widget_key(domain, channel)
    saved = _display_ids(domain, channel, settings, eodhd_cfg)
    # 外部 (另一标签页/手改文件) 改动后同步 widget 状态; 本页 on_change 已经
    # 落盘, 重读的 saved 与 session_state 一致, 这里不会回退用户刚拖好的顺序。
    if st.session_state.get(key) != saved:
        st.session_state[key] = saved

    is_custom = so.configured(domain, channel) is not None
    st.markdown(f"**{so.CHANNEL_LABELS[channel]}**")
    _component()(
        items=_items(domain, channel, eodhd_cfg, eodhd_ok),
        order=st.session_state.get(key) or saved,
        key=key,
        on_change=partial(_on_order_change, domain, channel, key),
    )
    current = st.session_state.get(key) or saved
    suffix = "（自定义）" if is_custom else "（默认）"
    st.caption(f"当前链路{suffix}: {so.chain_text(list(current))}")
    if is_custom:
        if st.button(
            "恢复默认顺序",
            key=f"{key}_reset",
            help="删除本通道的自定义顺序, 回到随数据源开关变化的默认链",
        ):
            _reset_order(domain, channel)
            st.rerun()


def render_source_order_editor(
    settings: dict, eodhd_cfg: dict, eodhd_ok: bool
) -> None:
    """在设置页渲染域/通道的拖动排序编辑器.

    用 st.radio 选择 provider, 而不是 st.tabs: 拖动会触发 rerun, radio 的
    选中值存在 session_state, 能稳定停留在用户正在编辑的 provider; 同时
    每次只渲染 1-2 个自定义组件, 比一次渲染 7 个 iframe 更轻。
    """
    domains = [d for d in so.DOMAIN_ORDER if so.CATALOG[d]]
    if not domains:
        return
    labels = [so.DOMAIN_META[d][0] for d in domains]
    selected = st.radio(
        "Provider",
        labels,
        index=0,
        horizontal=True,
        key="set_source_order_domain",
        label_visibility="collapsed",
    )
    domain = domains[labels.index(selected)]
    title, desc = so.DOMAIN_META[domain]
    st.caption(f"{title}: {desc}")
    if domain in ("global", "cn"):
        st.caption(
            "IBKR / 长桥是批量前置源，固定在最前，由上方开关控制；"
            "本列表只排序逐代码回退源。"
        )
    if domain == "global":
        st.caption("akshare 只对港股生效；非港股代码会自动跳过它。")
    if domain == "index":
        st.caption(
            "顺序只影响同时具备 akshare 与 yfinance 的指数；"
            "水泥网/恒生/Cameco/商品期货等专属源不参与排序。"
        )
    for channel in so.CHANNEL_ORDER:
        if channel not in so.CATALOG[domain] or not so.CATALOG[domain][channel]:
            continue
        _render_one(domain, channel, settings, eodhd_cfg, eodhd_ok)
