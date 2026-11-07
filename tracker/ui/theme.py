"""全站视觉层: 深色玻璃拟态主题 (global CSS + 页头组件).

Streamlit 自带控件只能靠 ``.streamlit/config.toml`` 调色, 版式/卡片/光效
仍需一层 CSS。本模块把主题集中到一处, 各页面只需:

    from tracker.ui import theme
    theme.inject()                     # app.py 每次渲染调用一次
    theme.page_header("组合总览", eyebrow="PORTFOLIO")

配色参照现代行情面板: 近黑底 + 青/紫霓虹点缀 + 玻璃卡片; 涨跌语义色仍由
``tracker.ui.settings`` 决定 (红涨绿跌 / 绿涨红跌), 本模块不覆盖。

选择器尽量只用 Streamlit 的 ``data-testid`` / ``st-key-*`` 稳定钩子, 避免
依赖版本易变的 emotion 类名; 升级 Streamlit 后个别样式失效不会影响功能。
"""

from __future__ import annotations

import html
import json

import streamlit as st

# 与 .streamlit/config.toml 保持同步的主色 (CSS 内无法读 config, 故常量集中于此)
BG = "#0a0a0f"
SURFACE = "#141428"
PRIMARY = "#00d4ff"
ACCENT = "#bf5af2"
UP_CN = "#ff453a"
DOWN_CN = "#32d74b"

# 页面主区最大宽度 (wide 布局下避免超宽屏元素被拉散)
_MAX_WIDTH = "1560px"


# ---------- 全局右键菜单 ----------
# Streamlit 的表格 (Glide Data Grid) 与图表默认弹浏览器原生菜单, 在数据面板里
# 既没有「复制整行」也没有「查看 K 线」, 与桌面行情软件的习惯不符。这里用
# components.v2 挂一个 0 高度的全局脚本组件:
#   * 表格: 右键落在可见单元格上时, 用无障碍表格里同位置的 td 反查行列与文本;
#   * 图表 (Plotly): 右键落在画布上时提供重置视图 / 下载图片;
#   * 输入框、正文等仍保留浏览器原生菜单 (复制/粘贴/翻译是用户预期行为)。
# 菜单 DOM 追加到 document.body, 复用本模块的全局 CSS, 避免被 ShadowRoot 裁剪。
_TABLE_CONTEXT_MENU_JS = r"""
export default function (component) {
  // rerun 会重新执行组件函数但不一定先调用上一次 cleanup: 用 window 级单例
  // 防止 document 上叠加多个 contextmenu 监听器。返回同一个 cleanup, 保证
  // Streamlit 真正卸载组件时仍能把监听器摘干净。
  if (window.__fpContextMenuCleanup) { return window.__fpContextMenuCleanup; }

  var MENU_ID = 'fp-context-menu';
  var TOAST_ID = 'fp-context-toast';
  // 组件的 trigger 通道: 删除等有副作用的操作交给 Python 处理 (确认弹窗 +
  // 锁内读-改-写), 前端只负责把「哪张表、哪一行」送出去。
  var setTriggerValue = component && component.setTriggerValue;
  var menu = null;
  var toastTimer = null;

  function make(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }

  function ensureMenu() {
    if (menu && document.body.contains(menu)) { return menu; }
    menu = document.createElement('div');
    menu.id = MENU_ID;
    menu.className = 'fp-ctx-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', '右键菜单');
    menu.tabIndex = -1;
    document.body.appendChild(menu);
    return menu;
  }

  function hideMenu() {
    if (menu) {
      // 直接移除节点 (而不是留一个空壳): 否则每次右键都会在 body 里
      // 堆积一个不可见的 #fp-context-menu。
      menu.remove();
      menu = null;
    }
  }

  function toast(text) {
    var el = document.getElementById(TOAST_ID);
    if (!el) {
      el = make('div', 'fp-ctx-toast');
      el.id = TOAST_ID;
      el.setAttribute('role', 'status');
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add('fp-ctx-show');
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () {
      el.classList.remove('fp-ctx-show');
    }, 1300);
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.top = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    ta.remove();
  }

  function copy(text) {
    if (!text) { return; }
    // 先给反馈再写剪贴板: 部分浏览器 (尤其 Firefox headless / 未授权)
    // 的 clipboard Promise 会长时间不落定, 等它再弹提示会让用户以为没生效。
    toast('已复制到剪贴板');
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).catch(function () {
        fallbackCopy(text);
      });
    } else {
      fallbackCopy(text);
    }
  }

  function icon(name) {
    var paths = {
      copy: '<rect x="9" y="9" width="11" height="11" rx="2"></rect>' +
            '<path d="M5 15V5a2 2 0 0 1 2-2h10"></path>',
      row: '<path d="M4 6h16M4 12h16M4 18h16"></path>',
      table: '<rect x="3" y="4" width="18" height="16" rx="2"></rect>' +
             '<path d="M3 10h18M9 4v16"></path>',
      chart: '<path d="M4 19V5M4 19h16"></path>' +
             '<path d="M8 15l3-4 3 2 4-6"></path>',
      trash: '<path d="M3 6h18"></path>' +
             '<path d="M8 6V4h8v2"></path>' +
             '<path d="M19 6l-1 14H6L5 6"></path>' +
             '<path d="M10 11v5M14 11v5"></path>'
    };
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' + (paths[name] || '') + '</svg>';
  }

  function showMenu(x, y, title, items) {
    var box = ensureMenu();
    box.innerHTML = '';
    if (title) {
      box.appendChild(make('div', 'fp-ctx-header', title));
    }
    items.forEach(function (item) {
      if (item.sep) {
        box.appendChild(make('div', 'fp-ctx-sep'));
        return;
      }
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fp-ctx-item';
      btn.setAttribute('role', 'menuitem');
      btn.innerHTML = icon(item.icon) + '<span></span>';
      btn.querySelector('span').textContent = item.label;
      if (item.disabled) {
        btn.disabled = true;
      } else {
        btn.addEventListener('click', function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          hideMenu();
          item.action();
        });
      }
      box.appendChild(btn);
    });
    // 先按左上角定位量出尺寸, 再贴边翻转, 避免菜单溢出窗口
    box.style.left = '0px';
    box.style.top = '0px';
    box.style.visibility = 'hidden';
    box.classList.add('fp-ctx-open');
    box.setAttribute('aria-hidden', 'false');
    var r = box.getBoundingClientRect();
    var left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
    var top = y + r.height > window.innerHeight - 8 ? y - r.height : y;
    top = Math.max(8, Math.min(top, window.innerHeight - r.height - 8));
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.style.visibility = 'visible';
    // 鼠标右键打开时不预选第一项 (与系统右键菜单一致); 键盘用户按
    // ArrowDown 再进入菜单项, 由 onKeyDown 处理。
  }

  // 右键落点对应的表: 用 st.dataframe/data_editor 的 key 区分持仓/自选。
  // 只在已知 key 上提供「删除」这类有副作用的操作, 其它表格保持通用菜单。
  var TABLE_KEYS = [
    'holdings_detail_select', 'holdings_editor',
    'watchlist_detail_select', 'watchlist_editor'
  ];
  function contextKey(df) {
    var el = df.closest('[class*="st-key-"]');
    if (!el) { return ''; }
    for (var i = 0; i < TABLE_KEYS.length; i++) {
      if (el.classList.contains('st-key-' + TABLE_KEYS[i])) {
        return TABLE_KEYS[i];
      }
    }
    return '';
  }

  function getCell(df, x, y) {
    var cells = df.querySelectorAll('[data-testid^="glide-cell-"]');
    var rowBest = null;
    var rowDist = Infinity;
    for (var i = 0; i < cells.length; i++) {
      var r = cells[i].getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        return cells[i];
      }
      // 行右侧空白/行号列也可能落点: 同一行内取水平距离最近的单元格,
      // 这样右键整行任意位置都能拿到 symbol, 不会掉回浏览器默认菜单。
      if (y >= r.top && y <= r.bottom) {
        var d = x < r.left ? r.left - x : (x > r.right ? x - r.right : 0);
        if (d < rowDist) { rowDist = d; rowBest = cells[i]; }
      }
    }
    return rowBest;
  }

  function cellParts(cell) {
    var m = /^glide-cell-(\d+)-(\d+)$/.exec(cell.getAttribute('data-testid') || '');
    return m ? { col: parseInt(m[1], 10), row: parseInt(m[2], 10) } : null;
  }

  function rowInfo(df, pos) {
    var cells = df.querySelectorAll(
      '[data-testid^="glide-cell-"][data-testid$="-' + pos.row + '"]'
    );
    var headers = {};
    var headersByAria = {};
    df.querySelectorAll('th[role="columnheader"]').forEach(function (th, i) {
      var text = (th.textContent || '').trim();
      // 优先用 DOM 顺序 (与数据列 0-based 顺序一致), 同时保留 aria-colindex
      // 作为兜底: Glide 的 aria-colindex 从 1 开始, 直接拿来查会整体错一列,
      // 导致「复制含表头」串列、识别不到「代码」列。
      headers[String(i)] = text;
      var idx = Number(th.getAttribute('aria-colindex'));
      if (isFinite(idx)) { headersByAria[String(idx)] = text; }
    });
    function headerFor(col) {
      // Glide 的 glide-cell-<col> 从 1 开始, th 的 DOM 顺序从 0 开始:
      // 直接 headers[col] 会整体错一列 (「symbol」列被认成「name」, 代码
      // 提取失败 → 右键菜单没有 K线/删除入口)。优先用 aria-colindex 对齐。
      return headersByAria[String(col)] || headers[String(col - 1)] ||
        headersByAria[String(col + 1)] || headers[String(col)] || '';
    }
    var values = [];
    var byCol = {};
    cells.forEach(function (c) {
      var p = cellParts(c);
      if (!p) { return; }
      var text = (c.textContent || '').trim();
      byCol[p.col] = text;
      values.push({ col: p.col, text: text });
    });
    values.sort(function (a, b) { return a.col - b.col; });
    var symbol = '';
    values.forEach(function (v) {
      var h = headerFor(v.col);
      if ((h === '代码' || h.toLowerCase() === 'symbol') && v.text) {
        symbol = v.text;
      }
    });
    return {
      values: values,
      headers: values.map(function (v) { return headerFor(v.col); }),
      symbol: symbol
    };
  }

  function sendContextAction(action, symbol) {
    if (!setTriggerValue || !symbol) { return; }
    // nonce 让 Python 能区分「同一条目再次删除」和上一次残留的 trigger 值
    setTriggerValue('ctx_action', JSON.stringify({
      action: action,
      symbol: symbol,
      nonce: Date.now() + ':' + Math.random().toString(36).slice(2)
    }));
  }

  function onContext(ev) {
    if (ev.defaultPrevented) { return; }
    var t = ev.target;
    if (!(t instanceof Element)) { return; }
    // 输入/编辑状态保留原生菜单: 复制、粘贴、拼写检查都在这里
    if (t.closest('input, textarea, [contenteditable="true"], [data-baseweb="input"]')) {
      return;
    }

    var cell = null;
    var df = t.closest('[data-testid="stDataFrame"], [data-testid="stDataEditor"]');
    if (df) {
      cell = getCell(df, ev.clientX, ev.clientY);
      if (!cell) { return; }
      var pos = cellParts(cell);
      if (!pos) { return; }
      var row = rowInfo(df, pos);
      var cellText = (cell.textContent || '').trim();
      var rowText = row.values.map(function (v) { return v.text; }).join('\t');
      var withHeader = row.headers.join('\t') + '\n' + rowText;
      var cellHeader = row.headers[row.values.findIndex(function (v) {
        return v.col === pos.col;
      })] || '';
      var title = (row.symbol || cellText) + (cellHeader ? '  ·  ' + cellHeader : '');
      var items = [
        {
          label: '复制单元格',
          icon: 'copy',
          disabled: !cellText,
          action: function () { copy(cellText); }
        },
        {
          label: '复制整行',
          icon: 'row',
          disabled: !rowText,
          action: function () { copy(rowText); }
        },
        {
          label: '复制含表头',
          icon: 'table',
          disabled: !rowText,
          action: function () { copy(withHeader); }
        }
      ];
      var tableKey = contextKey(df);
      if (row.symbol) {
        items.push({ sep: true });
        items.push({
          label: '查看 K 线',
          icon: 'chart',
          action: function () {
            window.location.assign(
              '/kline?symbol=' + encodeURIComponent(row.symbol) + '&t=' + Date.now()
            );
          }
        });
        if (tableKey === 'watchlist_detail_select' ||
            tableKey === 'watchlist_editor') {
          items.push({
            label: '从自选删除',
            icon: 'trash',
            action: function () {
              sendContextAction('delete_watchlist', row.symbol);
            }
          });
        } else if (tableKey === 'holdings_detail_select' ||
                   tableKey === 'holdings_editor') {
          items.push({
            label: '从持仓删除',
            icon: 'trash',
            action: function () {
              sendContextAction('delete_holdings', row.symbol);
            }
          });
        }
      }
      ev.preventDefault();
      ev.stopPropagation();
      showMenu(ev.clientX, ev.clientY, title, items);
      return;
    }

    // Plotly 图: 浏览器菜单里没有图表操作, 提供重置视图与导出图片
    var plot = t.closest('.js-plotly-plot');
    if (plot) {
      ev.preventDefault();
      ev.stopPropagation();
      var items = [
        {
          label: '重置视图',
          icon: 'chart',
          action: function () {
            try {
              var el = plot;
              var update = {};
              Object.keys(el._fullLayout || {}).forEach(function (k) {
                if (/^[xy]axis\d*$/.test(k)) {
                  update[k + '.autorange'] = true;
                }
              });
              if (window.Plotly && Object.keys(update).length) {
                window.Plotly.relayout(el, update);
              }
            } catch (e) {}
          }
        },
        {
          label: '下载图片 (PNG)',
          icon: 'table',
          action: function () {
            try {
              if (window.Plotly) {
                window.Plotly.downloadImage(plot, {
                  format: 'png',
                  filename: 'chart',
                  scale: 2
                });
              }
            } catch (e) {}
          }
        }
      ];
      showMenu(ev.clientX, ev.clientY, '图表', items);
    }
  }

  function onPointerDown(ev) {
    if (menu && !menu.contains(ev.target)) { hideMenu(); }
  }
  function onKeyDown(ev) {
    if (!menu) { return; }
    if (ev.key === 'Escape') {
      ev.preventDefault();
      hideMenu();
      return;
    }
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      var items = Array.prototype.slice.call(
        menu.querySelectorAll('.fp-ctx-item:not([disabled])')
      );
      if (!items.length) { return; }
      var idx = items.indexOf(document.activeElement);
      var next = ev.key === 'ArrowDown' ? idx + 1 : idx - 1;
      if (next < 0) { next = items.length - 1; }
      if (next >= items.length) { next = 0; }
      items[next].focus();
    }
  }
  function onViewportChange() { hideMenu(); }

  document.addEventListener('contextmenu', onContext, true);
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('scroll', onViewportChange, true);
  window.addEventListener('resize', onViewportChange);

  var cleanup = function () {
    document.removeEventListener('contextmenu', onContext, true);
    document.removeEventListener('pointerdown', onPointerDown, true);
    document.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('scroll', onViewportChange, true);
    window.removeEventListener('resize', onViewportChange);
    hideMenu();
    var t = document.getElementById(TOAST_ID);
    if (t) { t.remove(); }
    window.__fpContextMenuCleanup = null;
  };
  window.__fpContextMenuCleanup = cleanup;
  return cleanup;
}
"""

# 触控板双指横滑防误触后退: Chrome/Edge/Firefox 用 CSS
# (overscroll-behavior-x: none) 就能关掉浏览器历史导航, 但 Safari 不认这个
# 语义 (WebKit bug 240183), 只能拦 wheel。这里只在 Safari / 不支持
# overscroll-behavior 的浏览器上挂非 passive 监听; 其余浏览器保持合成器滚动,
# 避免全局 wheel 监听拖慢纵向滚动。
_TRACKPAD_GUARD_JS = r"""
export default function (component) {
  // rerun 会重跑组件函数但不一定先 cleanup: window 级单例避免叠加监听器
  if (window.__fpTrackpadGuardCleanup) { return window.__fpTrackpadGuardCleanup; }

  var cssSupported = !!(window.CSS && CSS.supports &&
    CSS.supports('overscroll-behavior-x', 'none'));
  var ua = navigator.userAgent || '';
  var isSafari = /safari/i.test(ua) &&
    !/chrome|chromium|crios|edg|opr|android/i.test(ua);
  if (cssSupported && !isSafari) {
    var noop = function () {};
    window.__fpTrackpadGuardCleanup = noop;
    return noop;
  }

  var THRESHOLD = 3;

  // 鼠标下仍有容器能沿手势方向横向滚动 → 保留原生滚动; 否则说明已到
  // 边界, 阻断默认行为, 浏览器就不会把继续横滑当成前进/后退。
  function canScrollHorizontally(path, deltaX) {
    for (var i = 0; i < path.length; i++) {
      var el = path[i];
      if (!el || el.nodeType !== 1) { continue; }
      var overflowX = window.getComputedStyle(el).overflowX;
      if (overflowX !== 'auto' && overflowX !== 'scroll' &&
          overflowX !== 'overlay') {
        continue;
      }
      var max = el.scrollWidth - el.clientWidth;
      if (max <= 1) { continue; }
      var left = el.scrollLeft;
      if (deltaX > 0 && left < max - 1) { return true; }
      if (deltaX < 0 && left > 1) { return true; }
    }
    return false;
  }

  function onWheel(ev) {
    // 捏合缩放 (ctrlKey) 和已有人处理的 wheel (如图表) 不再插手
    if (!ev.cancelable || ev.defaultPrevented || ev.ctrlKey) { return; }
    var dx = ev.deltaX;
    // Shift+滚轮 = 鼠标用户把纵向滚轮当横向滚动
    if (ev.shiftKey && Math.abs(ev.deltaY) > Math.abs(dx)) { dx = ev.deltaY; }
    if (Math.abs(dx) < THRESHOLD) { return; }
    if (!ev.shiftKey && Math.abs(dx) <= Math.abs(ev.deltaY)) { return; }
    var path = ev.composedPath ? ev.composedPath() : [ev.target];
    if (!canScrollHorizontally(path, dx)) { ev.preventDefault(); }
  }

  window.addEventListener('wheel', onWheel, { capture: true, passive: false });

  var cleanup = function () {
    window.removeEventListener('wheel', onWheel, { capture: true });
    window.__fpTrackpadGuardCleanup = null;
  };
  window.__fpTrackpadGuardCleanup = cleanup;
  return cleanup;
}
"""

_CONTEXT_MENU = None
_TRACKPAD_GUARD = None


def _context_menu():
    """惰性注册全局右键菜单组件, 并返回当前运行时可用的渲染器.

    不能在 import 时只注册一次: AppTest / 多 Runtime 场景下组件注册表可能在
    脚本运行前被清空, 之后挂载会报 "Component is not registered"。
    """
    global _CONTEXT_MENU
    from streamlit.components.v2 import get_bidi_component_manager

    if _CONTEXT_MENU is None or get_bidi_component_manager().get(
        "fp_context_menu"
    ) is None:
        _CONTEXT_MENU = st.components.v2.component(
            "fp_context_menu",
            js=_TABLE_CONTEXT_MENU_JS,
        )
    return _CONTEXT_MENU


def _trackpad_guard():
    """惰性注册触控板手势护栏组件 (Safari 兜底, 防止横滑误触后退)."""
    global _TRACKPAD_GUARD
    from streamlit.components.v2 import get_bidi_component_manager

    if _TRACKPAD_GUARD is None or get_bidi_component_manager().get(
        "fp_trackpad_guard"
    ) is None:
        _TRACKPAD_GUARD = st.components.v2.component(
            "fp_trackpad_guard",
            js=_TRACKPAD_GUARD_JS,
        )
    return _TRACKPAD_GUARD


# 右键菜单有副作用的操作 (删除) 经组件 trigger 回到 Python, 暂存在 session
# state, 由组合页在下一轮渲染时消费 (弹现有确认框 + 锁内读-改-写)。
CONTEXT_ACTION_KEY = "_ctx_action"
_CONTEXT_NONCE_KEY = "_ctx_action_nonce"


def _stash_context_action(result) -> None:
    """把组件 trigger 收到的右键操作写入 session_state (同一 trigger 只消费一次)."""
    raw = getattr(result, "ctx_action", None)
    if not raw:
        return
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    nonce = str(payload.get("nonce") or "")
    if nonce and st.session_state.get(_CONTEXT_NONCE_KEY) == nonce:
        return
    if nonce:
        st.session_state[_CONTEXT_NONCE_KEY] = nonce
    st.session_state[CONTEXT_ACTION_KEY] = payload


def inject() -> None:
    """注入全局主题样式 (幂等: 每次 rerun 重新渲染即可, 无状态副作用)."""
    st.html(f"<style>{_CSS}{_CONTEXT_MENU_CSS}</style>")
    # 0 高度脚本组件: 接管表格/图表的右键菜单, 自身不占版面
    result = _context_menu()(
        key="fp_context_menu",
        height=0,
        on_ctx_action_change=lambda: None,
    )
    _stash_context_action(result)
    # 触控板双指横滑不触发浏览器前进/后退 (尤其在侧边栏持仓表附近)
    _trackpad_guard()(key="fp_trackpad_guard", height=0)


def page_header(title: str, eyebrow: str = "", right_html: str = "") -> None:
    """渲染统一页头: 左侧眉题 + 大标题, 右侧可放状态胶囊.

    ``right_html`` 由调用方拼装 (时间戳/数据源等), 已是可信 HTML; 不处理
    用户输入, 避免把任意文本当标记渲染。
    """
    eyebrow_html = f'<div class="fp-eyebrow">{eyebrow}</div>' if eyebrow else ""
    right = f'<div class="fp-page-header-right">{right_html}</div>' if right_html else ""
    st.markdown(
        '<div class="fp-page-header">'
        f'<div class="fp-page-header-text">{eyebrow_html}'
        f'<h1 class="fp-page-title">{title}</h1></div>'
        f"{right}</div>",
        unsafe_allow_html=True,
    )


def chip(text: str, icon: str = "", title: str = "") -> str:
    """状态胶囊 HTML (配合 page_header 的 right_html 使用)."""
    icon_html = f'<span class="fp-chip-icon">{html.escape(icon)}</span>' if icon else ""
    title_attr = f' title="{html.escape(title)}"' if title else ""
    return (
        f'<span class="fp-chip"{title_attr}>'
        f"{icon_html}{html.escape(text)}</span>"
    )


# 图表分类配色: 青-紫-琥珀-绿-红-靛-灰, 与页面霓虹主色一致 (首色避开涨跌红绿)
CHART_COLORS = [
    "#00d4ff",
    "#bf5af2",
    "#ff9f0a",
    "#32d74b",
    "#ff453a",
    "#7c6cff",
    "#f7d774",
    "#94a3b8",
]


def style_chart(fig, *, transparent: bool = True):
    """给 Plotly 图套上统一深色外观 (透明背景可透出页面光晕)."""
    layout = dict(
        template="plotly_dark",
        colorway=CHART_COLORS,
        font=dict(
            family=_FONT_STACK,
            size=12,
            color="rgba(246, 247, 255, 0.78)",
        ),
        legend=dict(
            bgcolor="rgba(0, 0, 0, 0)",
            font=dict(size=11),
        ),
    )
    if transparent:
        layout["paper_bgcolor"] = "rgba(0, 0, 0, 0)"
        layout["plot_bgcolor"] = "rgba(0, 0, 0, 0)"
    # 只给已有标题的图补样式: 无标题时塞一个只有样式的 title 对象, plotly
    # 会把空 text 渲染成字面量 "undefined" (图中央凭空多一行字)。
    if fig.layout.title.text:
        layout["title"] = dict(
            text=fig.layout.title.text,
            font=dict(size=13, color="rgba(246, 247, 255, 0.86)"),
            x=0.5,
            xanchor="center",
        )
    fig.update_layout(**layout)
    return fig


_FONT_STACK = (
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "'PingFang SC', 'Microsoft YaHei', sans-serif"
)


# 右键菜单样式独立成串, 避免塞进 f-string 后每个 CSS 花括号都要写两遍。
# 菜单挂在 document.body 上 (非 ShadowRoot), 因此这里的选择器是全局作用域。
_CONTEXT_MENU_CSS = """
.fp-ctx-menu {
  position: fixed;
  z-index: 2147483000;
  min-width: 188px;
  max-width: min(320px, calc(100vw - 16px));
  padding: 6px;
  border: 1px solid var(--fp-border-strong);
  border-radius: 12px;
  background: rgba(18, 18, 34, 0.97);
  box-shadow: 0 22px 56px -18px rgba(0, 0, 0, 0.9),
    0 0 0 1px rgba(0, 0, 0, 0.25);
  backdrop-filter: blur(20px) saturate(140%);
  color: var(--fp-text);
  font-family: var(--fp-sans);
  font-size: 13px;
  line-height: 1.35;
  user-select: none;
  opacity: 0;
  transform: translateY(-3px) scale(0.985);
  transform-origin: top left;
  transition: opacity 110ms ease, transform 110ms ease;
}
.fp-ctx-menu.fp-ctx-open {
  opacity: 1;
  transform: none;
}
.fp-ctx-header {
  padding: 5px 10px 7px;
  margin-bottom: 4px;
  border-bottom: 1px solid var(--fp-border);
  color: var(--fp-text-2);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fp-ctx-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--fp-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 110ms ease, color 110ms ease;
}
.fp-ctx-item svg {
  width: 15px;
  height: 15px;
  flex: none;
  color: var(--fp-cyan);
  opacity: 0.82;
}
.fp-ctx-item:hover,
.fp-ctx-item:focus-visible {
  background: rgba(0, 212, 255, 0.13);
  color: #ffffff;
  outline: none;
}
.fp-ctx-item:hover svg,
.fp-ctx-item:focus-visible svg { opacity: 1; }
.fp-ctx-item:disabled {
  opacity: 0.38;
  cursor: default;
  background: transparent;
}
.fp-ctx-sep {
  height: 1px;
  margin: 5px 8px;
  background: var(--fp-border);
}
.fp-ctx-toast {
  position: fixed;
  left: 50%;
  bottom: 32px;
  z-index: 2147483001;
  padding: 8px 16px;
  border: 1px solid var(--fp-border-strong);
  border-radius: 999px;
  background: rgba(18, 18, 34, 0.96);
  box-shadow: 0 16px 40px -16px rgba(0, 0, 0, 0.9);
  color: var(--fp-text);
  font-family: var(--fp-sans);
  font-size: 12px;
  pointer-events: none;
  opacity: 0;
  transform: translate(-50%, 8px);
  transition: opacity 160ms ease, transform 160ms ease;
}
.fp-ctx-toast.fp-ctx-show {
  opacity: 1;
  transform: translate(-50%, 0);
}
"""


_CSS = f"""
:root {{
  --fp-bg: {BG};
  --fp-surface: {SURFACE};
  --fp-border: rgba(255, 255, 255, 0.09);
  --fp-border-strong: rgba(255, 255, 255, 0.16);
  --fp-text: #f6f7ff;
  --fp-text-2: rgba(246, 247, 255, 0.66);
  --fp-text-3: rgba(246, 247, 255, 0.4);
  --fp-cyan: {PRIMARY};
  --fp-purple: {ACCENT};
  --fp-amber: #ff9f0a;
  --fp-radius: 16px;
  --fp-radius-sm: 11px;
  --fp-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", ui-monospace,
    "SFMono-Regular", Menlo, Consolas, monospace;
  --fp-sans: "Inter", "SF Pro Text", -apple-system, BlinkMacSystemFont,
    "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}}

/* ---------- 背景: 近黑底 + 两团低透明度霓虹光晕 ---------- */
html, body, .stApp {{
  background-color: var(--fp-bg) !important;
  color: var(--fp-text);
}}
.stApp {{
  background-image:
    radial-gradient(1100px 620px at 8% -12%, rgba(0, 212, 255, 0.11), transparent 62%),
    radial-gradient(900px 540px at 96% -4%, rgba(191, 90, 242, 0.12), transparent 58%),
    radial-gradient(700px 700px at 50% 115%, rgba(0, 212, 255, 0.045), transparent 65%),
    linear-gradient(180deg, #0a0a10 0%, #0b0b15 48%, #08080e 100%) !important;
  background-attachment: fixed;
}}
[data-testid="stAppViewContainer"] {{ background: transparent; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stAppViewContainer"] > .main {{ background: transparent; }}

/* ---------- 触控板: 双指横滑不触发浏览器前进/后退 ---------- */
/* 浏览器把「横向滚到边界后继续滑」当成历史导航。持仓表/自选表窄, 两指
   横滑很容易在左边界误触后退 —— 根节点关掉历史滑动, 所有滚动容器禁止
   横向滚动链; 容器内部横向滚动本身不受影响。Safari 不认这条 CSS, 由
   _TRACKPAD_GUARD_JS 的 wheel 兜底。 */
html, body {{ overscroll-behavior-x: none; }}
* {{ overscroll-behavior-x: contain; }}

/* ---------- 主区容器: 统一留白, 超宽屏居中 ---------- */
[data-testid="stMainBlockContainer"] {{
  max-width: {_MAX_WIDTH};
  margin: 0 auto;
  padding-top: 4.75rem;
  padding-bottom: 4rem;
}}
[data-testid="stVerticalBlock"] {{ gap: 0.9rem; }}

/* ---------- 顶部工具条: 玻璃 + 细边 ---------- */
[data-testid="stHeader"] {{
  background: linear-gradient(180deg, rgba(12, 12, 20, 0.82), rgba(10, 10, 16, 0.55)) !important;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}}
[data-testid="stHeader"]::after {{
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(0, 212, 255, 0.45), rgba(191, 90, 242, 0.35), transparent);
  opacity: 0.65;
}}

/* ---------- 侧边栏: 玻璃面板 + 胶囊导航 ---------- */
[data-testid="stSidebar"] {{
  background:
    radial-gradient(600px 320px at 0% 0%, rgba(0, 212, 255, 0.07), transparent 60%),
    linear-gradient(180deg, rgba(18, 18, 32, 0.92), rgba(9, 9, 15, 0.97)) !important;
  border-right: 1px solid var(--fp-border);
  backdrop-filter: blur(24px) saturate(150%);
}}
[data-testid="stSidebarContent"] {{ padding-top: 0.35rem; }}
[data-testid="stSidebarNav"] {{ padding: 0.15rem 0.55rem 0.35rem; }}
[data-testid="stSidebarNavLinkContainer"] {{ border-radius: 10px; }}
[data-testid="stSidebarNavLink"] {{
  border-radius: 10px;
  padding-top: 0.42rem;
  padding-bottom: 0.42rem;
  color: var(--fp-text-2);
  transition: background 160ms ease, color 160ms ease, transform 160ms ease;
}}
[data-testid="stSidebarNavLink"]:hover {{
  background: rgba(255, 255, 255, 0.055);
  color: var(--fp-text);
}}
[data-testid="stSidebarNavLink"][aria-current="page"] {{
  background: linear-gradient(135deg, rgba(0, 212, 255, 0.16), rgba(191, 90, 242, 0.13));
  box-shadow: inset 0 0 0 1px rgba(0, 212, 255, 0.24);
  color: #ffffff;
}}
[data-testid="stSidebarHeader"] {{ padding-bottom: 0.35rem; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0.35rem; }}
[data-testid="stSidebar"] hr {{ border-color: var(--fp-border); }}

/* ---------- 标题 / 文本 ---------- */
h1, h2, h3, h4 {{
  font-family: var(--fp-sans);
  letter-spacing: -0.018em;
  color: var(--fp-text);
}}
[data-testid="stMarkdownContainer"] p {{ color: rgba(246, 247, 255, 0.82); }}
[data-testid="stCaptionContainer"] p,
[data-testid="stCaptionContainer"] {{
  color: var(--fp-text-3);
  font-size: 0.78rem;
  line-height: 1.55;
}}
[data-testid="stMarkdownContainer"] h3 {{
  font-size: 1.22rem;
  font-weight: 650;
  margin-bottom: 0.35rem;
}}

/* ---------- 页头 ---------- */
.fp-page-header {{
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  margin: 0.15rem 0 0.35rem;
}}
.fp-eyebrow {{
  font-family: var(--fp-mono);
  font-size: 0.64rem;
  font-weight: 600;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: rgba(0, 212, 255, 0.72);
  margin-bottom: 0.3rem;
}}
.fp-page-title {{
  margin: 0;
  font-family: var(--fp-sans);
  font-size: clamp(1.5rem, 1.15rem + 1.4vw, 2.05rem);
  font-weight: 720;
  letter-spacing: -0.03em;
  line-height: 1.12;
  background: linear-gradient(100deg, #ffffff 0%, #d9e6ff 45%, #a9d9ff 78%, #e3c7ff 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.fp-page-header-right {{ display: flex; align-items: center; gap: 0.5rem; }}
/* 查看范围下拉: 不占满整行, 留出呼吸感 (窄屏仍全宽) */
.st-key-watch_scope {{ max-width: 380px; }}
.fp-chip {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.34rem 0.72rem;
  border-radius: 999px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.045);
  color: var(--fp-text-2);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  backdrop-filter: blur(12px);
}}
.fp-chip::before {{
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fp-cyan);
  box-shadow: 0 0 8px rgba(0, 212, 255, 0.9);
}}
.fp-chip-icon {{ color: var(--fp-cyan); }}

/* ---------- 卡片: 所有 border=True 容器 + metric ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {{
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.055), rgba(0, 212, 255, 0.018) 48%, rgba(191, 90, 242, 0.032));
  border: 1px solid var(--fp-border) !important;
  border-radius: var(--fp-radius) !important;
  box-shadow:
    0 18px 40px -22px rgba(0, 0, 0, 0.85),
    inset 0 1px 0 rgba(255, 255, 255, 0.055);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  transition: border-color 200ms ease, box-shadow 200ms ease;
}}
[data-testid="stVerticalBlockBorderWrapper"]:hover {{
  border-color: rgba(0, 212, 255, 0.2) !important;
}}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlockBorderWrapper"] {{
  background: rgba(255, 255, 255, 0.025);
  box-shadow: none;
}}

[data-testid="stMetric"] {{
  position: relative;
  overflow: hidden;
  padding: 1rem 1.15rem 0.95rem;
  border-radius: var(--fp-radius);
  border: 1px solid var(--fp-border);
  background:
    radial-gradient(120% 90% at 0% 0%, rgba(0, 212, 255, 0.075), transparent 55%),
    linear-gradient(150deg, rgba(255, 255, 255, 0.055), rgba(20, 20, 40, 0.5));
  box-shadow:
    0 18px 40px -24px rgba(0, 0, 0, 0.9),
    inset 0 1px 0 rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(16px);
  transition: transform 220ms ease, border-color 220ms ease, box-shadow 220ms ease;
}}
[data-testid="stMetric"]::after {{
  content: "";
  position: absolute;
  top: 0;
  left: 12%;
  right: 12%;
  height: 1.5px;
  background: linear-gradient(90deg, transparent, rgba(0, 212, 255, 0.75), rgba(191, 90, 242, 0.55), transparent);
  opacity: 0.8;
}}
[data-testid="stMetric"]:hover {{
  transform: translateY(-2px);
  border-color: rgba(0, 212, 255, 0.3);
  box-shadow:
    0 26px 48px -26px rgba(0, 0, 0, 0.95),
    0 0 0 1px rgba(0, 212, 255, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.08);
}}
[data-testid="stMetricLabel"] {{
  color: var(--fp-text-2) !important;
  font-size: 0.74rem !important;
  font-weight: 550;
  letter-spacing: 0.06em;
}}
[data-testid="stMetricValue"] {{
  font-family: var(--fp-mono);
  font-size: clamp(1.35rem, 1.05rem + 0.9vw, 1.85rem) !important;
  font-weight: 700;
  letter-spacing: -0.035em;
  color: #ffffff;
  font-variant-numeric: tabular-nums;
}}
[data-testid="stMetricDelta"] {{
  font-family: var(--fp-mono);
  font-size: 0.76rem !important;
  font-variant-numeric: tabular-nums;
}}
/* 首页 KPI 首卡 (总市值): 青色描边更醒目 */
.st-key-fp_kpi [data-testid="stMetric"]:first-child {{
  border-color: rgba(0, 212, 255, 0.28);
  background:
    radial-gradient(130% 100% at 0% 0%, rgba(0, 212, 255, 0.14), transparent 58%),
    linear-gradient(150deg, rgba(255, 255, 255, 0.06), rgba(16, 24, 42, 0.55));
}}
.st-key-fp_kpi [data-testid="stMetric"]:first-child [data-testid="stMetricValue"] {{
  font-size: clamp(1.55rem, 1.15rem + 1.15vw, 2.15rem) !important;
}}

/* ---------- 标签页: 胶囊轨道 (兼容 1.65 react-aria 与旧 baseweb 结构) ---------- */
[data-testid="stTabs"] [role="tablist"],
[data-testid="stTabs"] [data-baseweb="tab-list"] {{
  gap: 0.3rem;
  padding: 0.3rem;
  width: fit-content;
  max-width: 100%;
  border-radius: 13px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(14px);
}}
[data-testid="stTab"],
[data-testid="stTabs"] [data-baseweb="tab"] {{
  border-radius: 9px;
  padding: 0.38rem 0.95rem;
  color: var(--fp-text-2);
  font-size: 0.84rem;
  transition: background 160ms ease, color 160ms ease;
}}
[data-testid="stTab"]:hover,
[data-testid="stTabs"] [data-baseweb="tab"]:hover {{
  background: rgba(255, 255, 255, 0.05);
  color: var(--fp-text);
}}
[data-testid="stTab"][data-selected="true"],
[data-testid="stTabs"] [aria-selected="true"][data-baseweb="tab"] {{
  background: linear-gradient(135deg, rgba(0, 212, 255, 0.18), rgba(191, 90, 242, 0.16));
  color: #ffffff;
  box-shadow: inset 0 0 0 1px rgba(0, 212, 255, 0.22);
}}
[data-testid="stTabs"] .react-aria-SelectionIndicator,
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] {{
  display: none;
}}
[data-testid="stTabPanel"],
[data-testid="stTabs"] [role="tabpanel"] {{ padding-top: 0.9rem; }}

/* ---------- 按钮 ---------- */
.stButton > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {{
  border-radius: 10px;
  border: 1px solid var(--fp-border-strong);
  background: rgba(255, 255, 255, 0.045);
  color: var(--fp-text);
  font-weight: 550;
  transition: transform 150ms ease, border-color 150ms ease, box-shadow 150ms ease,
    background 150ms ease;
}}
.stButton > button:hover,
[data-testid="stDownloadButton"] > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {{
  border-color: rgba(0, 212, 255, 0.5);
  background: rgba(0, 212, 255, 0.08);
  box-shadow: 0 0 18px -6px rgba(0, 212, 255, 0.55);
  color: #ffffff;
}}
.stButton > button:active,
[data-testid="stDownloadButton"] > button:active {{ transform: scale(0.985); }}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {{
  border: none;
  background: linear-gradient(135deg, #22e0ff, #7d7bff 55%, #bf5af2);
  color: #051019;
  font-weight: 650;
  box-shadow: 0 10px 26px -12px rgba(0, 212, 255, 0.8);
}}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {{
  color: #051019;
  box-shadow: 0 14px 32px -12px rgba(0, 212, 255, 0.95);
  filter: brightness(1.05);
}}

/* ---------- 输入类控件 ---------- */
[data-baseweb="input"],
[data-baseweb="textarea"],
[data-baseweb="select"] > div,
[data-testid="stNumberInput"] [data-baseweb="input"],
[data-testid="stTextInput"] [data-baseweb="input"] {{
  background: rgba(255, 255, 255, 0.04) !important;
  border-color: var(--fp-border) !important;
  border-radius: 10px !important;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}}
[data-baseweb="input"]:focus-within,
[data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {{
  border-color: rgba(0, 212, 255, 0.55) !important;
  box-shadow: 0 0 0 3px rgba(0, 212, 255, 0.11) !important;
}}
[data-baseweb="select"] [data-baseweb="popover"] [role="listbox"] {{
  background: #14142a;
  border: 1px solid var(--fp-border);
  border-radius: 12px;
}}
[data-testid="stWidgetLabel"] p {{
  color: var(--fp-text-2);
  font-size: 0.8rem;
  font-weight: 500;
}}
[data-testid="stRadio"] [role="radiogroup"] {{ gap: 0.3rem; }}

/* ---------- 表格 ---------- */
[data-testid="stDataFrame"],
[data-testid="stDataEditor"] {{
  border: 1px solid var(--fp-border);
  border-radius: 13px;
  overflow: hidden;
  box-shadow: 0 16px 36px -26px rgba(0, 0, 0, 0.9);
}}
[data-testid="stDataFrame"] [data-testid="stElementToolbar"] {{ border-radius: 8px; }}

/* ---------- 提示条 / 折叠面板 ---------- */
[data-testid="stAlert"] {{
  border-radius: 12px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.04);
  backdrop-filter: blur(12px);
}}
[data-testid="stExpander"] {{
  border: 1px solid var(--fp-border);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.028);
  overflow: hidden;
  backdrop-filter: blur(12px);
}}
[data-testid="stExpander"] summary {{ border-radius: 12px; }}
[data-testid="stExpander"] summary:hover {{ background: rgba(255, 255, 255, 0.04); }}

/* ---------- 图表容器去白边 ---------- */
[data-testid="stPlotlyChart"] {{ border-radius: 12px; overflow: hidden; }}

/* ---------- 财经日历: 概览 / 月历卡 / 日程 ---------- */
/* 事件类型主色: 财报=青, 除权除息=琥珀, 股权登记=紫, 派息=绿 */
.fp-ev-earnings {{ --ev: #00d4ff; --ev-bg: rgba(0, 212, 255, 0.12); --ev-fg: #a9eeff; }}
.fp-ev-ex_dividend {{ --ev: #ff9f0a; --ev-bg: rgba(255, 159, 10, 0.13); --ev-fg: #ffd9a1; }}
.fp-ev-record_date {{ --ev: #bf5af2; --ev-bg: rgba(191, 90, 242, 0.14); --ev-fg: #e3c1ff; }}
.fp-ev-dividend {{ --ev: #32d74b; --ev-bg: rgba(50, 215, 75, 0.12); --ev-fg: #b9f7c1; }}
.fp-ev-other {{ --ev: #94a3b8; --ev-bg: rgba(148, 163, 184, 0.12); --ev-fg: #cbd5e1; }}
.fp-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ev, #94a3b8);
  box-shadow: 0 0 8px -1px var(--ev, #94a3b8);
  flex: none;
}}

/* 概览条: 统计卡 + 事件图例 */
.fp-cal-overview {{
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 0.5rem;
  margin: 0.55rem 0 0.95rem;
}}
.fp-cal-stat {{
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 0.1rem;
  min-width: 104px;
  padding: 0.5rem 0.85rem;
  border: 1px solid var(--fp-border);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(12px);
}}
.fp-cal-stat-v {{
  font-family: var(--fp-mono);
  font-size: 1.02rem;
  font-weight: 700;
  line-height: 1.2;
  color: #ffffff;
  font-variant-numeric: tabular-nums;
}}
.fp-cal-stat-l {{
  font-size: 0.66rem;
  letter-spacing: 0.06em;
  color: var(--fp-text-3);
}}
.fp-cal-legend {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem 0.95rem;
  margin-left: auto;
  padding: 0.55rem 0.95rem;
  border: 1px solid var(--fp-border);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.02);
}}
.fp-cal-legend-item {{
  display: inline-flex;
  align-items: center;
  gap: 0.38rem;
  font-size: 0.72rem;
  color: var(--fp-text-2);
  white-space: nowrap;
}}
.fp-cal-legend-item b {{
  font-family: var(--fp-mono);
  font-weight: 600;
  color: var(--fp-text);
}}

/* 月历卡: 玻璃底板 + 头部标题 */
.fp-cal-month {{
  margin: 0.2rem 0 1.3rem;
  padding: 1rem 1.05rem 1.1rem;
  border: 1px solid var(--fp-border);
  border-radius: var(--fp-radius);
  background:
    linear-gradient(160deg, rgba(255, 255, 255, 0.04), rgba(0, 212, 255, 0.012) 45%, rgba(191, 90, 242, 0.024));
  box-shadow:
    0 20px 44px -30px rgba(0, 0, 0, 0.9),
    inset 0 1px 0 rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
}}
.fp-cal-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
  margin: 0 0 0.8rem;
}}
.fp-cal-title {{
  font-size: 1.02rem;
  font-weight: 650;
  letter-spacing: -0.012em;
  color: var(--fp-text);
}}
.fp-cal-month-meta {{
  font-family: var(--fp-mono);
  font-size: 0.7rem;
  color: var(--fp-text-3);
}}
.fp-cal-grid {{
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 5px;
}}
.fp-cal-wd {{
  padding: 0.05rem 0 0.4rem;
  text-align: center;
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--fp-text-3);
}}
.fp-cal-wd.is-weekend {{ color: rgba(255, 159, 10, 0.72); }}
.fp-cal-cell {{
  display: flex;
  flex-direction: column;
  min-height: 120px;
  padding: 6px 6px 7px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.024);
  overflow: hidden;
  transition: background 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
}}
.fp-cal-cell:hover {{
  background: rgba(255, 255, 255, 0.05);
  border-color: var(--fp-border);
}}
.fp-cal-cell.is-weekend {{ background: rgba(255, 255, 255, 0.014); }}
.fp-cal-cell.is-out {{
  background: transparent;
  border-color: transparent;
}}
.fp-cal-cell.is-out .fp-cal-num {{ color: rgba(246, 247, 255, 0.18); }}
.fp-cal-cell.has-ev {{
  border-color: rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.035);
}}
.fp-cal-cell.is-today {{
  border-color: rgba(0, 212, 255, 0.5);
  background:
    radial-gradient(130% 100% at 18% 0%, rgba(0, 212, 255, 0.14), rgba(255, 255, 255, 0.02) 62%);
  box-shadow:
    0 0 0 1px rgba(0, 212, 255, 0.12),
    0 14px 34px -24px rgba(0, 212, 255, 0.8);
}}
.fp-cal-num {{
  display: flex;
  align-items: center;
  justify-content: center;
  width: 1.55rem;
  height: 1.55rem;
  margin-bottom: 3px;
  border-radius: 50%;
  font-family: var(--fp-mono);
  font-size: 0.73rem;
  font-weight: 600;
  line-height: 1;
  color: var(--fp-text-2);
  font-variant-numeric: tabular-nums;
}}
.fp-cal-cell.has-ev .fp-cal-num {{ color: var(--fp-text); }}
.fp-cal-cell.is-today .fp-cal-num {{
  background: linear-gradient(135deg, #22e0ff, #7d7bff 68%, #bf5af2);
  color: #04121a;
  font-weight: 800;
  box-shadow: 0 0 18px -4px rgba(0, 212, 255, 0.85);
}}
.fp-cal-evs {{
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}}
.fp-cal-ev {{
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  padding: 2px 5px;
  border-radius: 5px;
  background: var(--ev-bg, rgba(255, 255, 255, 0.06));
  color: var(--ev-fg, var(--fp-text-2));
}}
.fp-cal-ev .fp-dot {{ width: 5px; height: 5px; }}
.fp-cal-ev-text {{
  flex: none;
  font-family: var(--fp-mono);
  font-size: 0.62rem;
  line-height: 1.4;
  white-space: nowrap;
}}
.fp-cal-name {{
  flex: 1;
  min-width: 0;
  font-size: 0.62rem;
  line-height: 1.4;
  color: var(--fp-text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.fp-cal-more {{
  margin-top: auto;
  padding: 2px 2px 0 4px;
  font-size: 0.6rem;
  color: var(--fp-text-3);
}}
/* 窄屏用类型色点代替被截断的代码 (完整信息见下方日程/表格) */
.fp-cal-marks {{ display: none; }}
.fp-cal-tag {{
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ev, #94a3b8);
  box-shadow: 0 0 6px -1px var(--ev, #94a3b8);
}}
.fp-cal-tag-more {{
  font-size: 0.52rem;
  line-height: 1;
  color: var(--fp-text-3);
}}

/* 小节标题 (日程等) */
.fp-section-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
  margin: 0.35rem 0 0.55rem;
}}
.fp-section-title {{
  font-size: 1.02rem;
  font-weight: 650;
  letter-spacing: -0.012em;
  color: var(--fp-text);
}}
.fp-section-sub {{
  font-family: var(--fp-mono);
  font-size: 0.7rem;
  color: var(--fp-text-3);
}}

/* 日程 (agenda): 左侧日期栏 + 右侧事件条目 */
.fp-agenda {{
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 1.15rem;
}}
.fp-agenda-day {{
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 0.85rem;
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--fp-border);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.024);
  transition: border-color 160ms ease, background 160ms ease;
}}
.fp-agenda-day:hover {{
  border-color: var(--fp-border-strong);
  background: rgba(255, 255, 255, 0.035);
}}
.fp-agenda-day.is-today {{
  border-color: rgba(0, 212, 255, 0.38);
  background:
    radial-gradient(120% 100% at 0% 0%, rgba(0, 212, 255, 0.1), rgba(255, 255, 255, 0.02) 60%);
}}
.fp-agenda-day.is-past {{ opacity: 0.62; }}
.fp-agenda-date {{
  display: flex;
  flex-direction: column;
  gap: 0.08rem;
  padding-top: 0.1rem;
}}
.fp-agenda-dow {{
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--fp-cyan);
}}
.fp-agenda-num {{
  font-family: var(--fp-mono);
  font-size: 0.98rem;
  font-weight: 700;
  line-height: 1.25;
  color: var(--fp-text);
  font-variant-numeric: tabular-nums;
}}
.fp-agenda-rel {{
  font-size: 0.64rem;
  color: var(--fp-text-3);
}}
.fp-agenda-items {{
  display: flex;
  flex-direction: column;
  gap: 0.32rem;
  min-width: 0;
}}
.fp-agenda-item {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem 0.55rem;
  min-width: 0;
  padding: 0.32rem 0.5rem;
  border-radius: 8px;
  border-left: 2px solid var(--ev, #94a3b8);
  background: var(--ev-bg, rgba(255, 255, 255, 0.04));
}}
.fp-agenda-badge {{
  padding: 0.02rem 0.38rem;
  border-radius: 4px;
  font-size: 0.64rem;
  font-weight: 600;
  white-space: nowrap;
  background: rgba(255, 255, 255, 0.09);
  color: var(--ev-fg, var(--fp-text-2));
}}
.fp-agenda-symbol {{
  font-family: var(--fp-mono);
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--fp-text);
  white-space: nowrap;
}}
.fp-agenda-name {{
  font-size: 0.74rem;
  color: var(--fp-text-2);
}}
.fp-agenda-status {{
  padding: 0.02rem 0.36rem;
  border-radius: 4px;
  font-size: 0.62rem;
  white-space: nowrap;
  color: var(--fp-text-3);
  background: rgba(255, 255, 255, 0.05);
}}
.fp-agenda-note {{
  flex: 1;
  min-width: 8rem;
  font-size: 0.7rem;
  color: var(--fp-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.fp-agenda-src {{
  margin-left: auto;
  font-family: var(--fp-mono);
  font-size: 0.6rem;
  color: rgba(246, 247, 255, 0.28);
  white-space: nowrap;
}}

/* ---------- 滚动条 ---------- */
* {{ scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, 0.16) transparent; }}
::-webkit-scrollbar {{ width: 9px; height: 9px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{
  background: rgba(255, 255, 255, 0.14);
  border-radius: 999px;
  border: 2px solid transparent;
  background-clip: content-box;
}}
::-webkit-scrollbar-thumb:hover {{ background: rgba(0, 212, 255, 0.4); background-clip: content-box; }}

/* ---------- 窄屏 ---------- */
@media (max-width: 640px) {{
  [data-testid="stMainBlockContainer"] {{ padding-top: 3.75rem; }}
  .st-key-watch_scope {{ max-width: none; }}
  .fp-page-header {{ align-items: flex-start; }}
  .fp-page-header-right {{ flex-wrap: wrap; }}
  .fp-page-title {{ font-size: 1.45rem; }}
  [data-testid="stMetric"] {{ padding: 0.8rem 0.9rem; }}
  [data-testid="stMetricValue"] {{ font-size: 1.3rem !important; }}
  [data-testid="stTabs"] [role="tablist"],
  [data-testid="stTabs"] [data-baseweb="tab-list"] {{
    width: 100%;
    overflow-x: auto;
    flex-wrap: nowrap;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }}
  [data-testid="stTabs"] [role="tablist"]::-webkit-scrollbar,
  [data-testid="stTabs"] [data-baseweb="tab-list"]::-webkit-scrollbar {{
    display: none;
  }}
  [data-testid="stTab"],
  [data-testid="stTabs"] [data-baseweb="tab"] {{
    flex: 0 0 auto;
    padding: 0.36rem 0.7rem;
    font-size: 0.78rem;
  }}
  /* 导入页 4 个来源标签: 折成 2×2, 避免最后一个被横向裁切 */
  .st-key-import_source_tabs [data-testid="stTabs"] [role="tablist"] {{
    flex-wrap: wrap;
    overflow: visible;
    row-gap: 0.25rem;
  }}
  .st-key-import_source_tabs [data-testid="stTab"] {{
    flex: 1 1 calc(50% - 0.35rem);
    justify-content: center;
  }}
  /* 日历: 7 列固定, 窄屏只留类型色点 (完整信息见下方日程/表格) */
  .fp-cal-overview {{ gap: 0.4rem; margin: 0.45rem 0 0.8rem; }}
  .fp-cal-stat {{ min-width: 86px; padding: 0.42rem 0.65rem; }}
  .fp-cal-stat-v {{ font-size: 0.9rem; }}
  .fp-cal-legend {{
    width: 100%;
    margin-left: 0;
    padding: 0.45rem 0.7rem;
    gap: 0.3rem 0.7rem;
  }}
  .fp-cal-month {{ padding: 0.7rem 0.55rem 0.8rem; border-radius: 12px; margin-bottom: 0.9rem; }}
  .fp-cal-head {{ margin-bottom: 0.5rem; }}
  .fp-cal-title {{ font-size: 0.88rem; }}
  .fp-cal-month-meta {{ font-size: 0.62rem; }}
  .fp-cal-grid {{ gap: 2px; }}
  .fp-cal-wd {{ padding: 0.05rem 0 0.25rem; font-size: 0.58rem; letter-spacing: 0; }}
  .fp-cal-cell {{ min-height: 62px; padding: 3px 3px 4px; border-radius: 6px; }}
  .fp-cal-num {{ width: 1.3rem; height: 1.3rem; margin-bottom: 1px; font-size: 0.62rem; }}
  .fp-cal-evs {{ display: none; }}
  .fp-cal-name {{ display: none; }}
  .fp-cal-more {{ display: none; }}
  .fp-cal-marks {{
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    margin-top: 2px;
  }}
  .fp-agenda-day {{
    grid-template-columns: 56px minmax(0, 1fr);
    gap: 0.55rem;
    padding: 0.55rem 0.6rem;
  }}
  .fp-agenda-num {{ font-size: 0.86rem; }}
  .fp-agenda-note {{ flex-basis: 100%; white-space: normal; }}
  .fp-agenda-src {{ margin-left: 0; }}
}}
"""
