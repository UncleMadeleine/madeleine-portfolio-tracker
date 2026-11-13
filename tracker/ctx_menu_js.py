"""右键菜单公共 JS 片段 (theme 表格菜单 / charting K线菜单共用).

不依赖 Streamlit: charting.py 也会在 CLI 图表组件里使用, 不能 import
tracker.ui(其 __init__ 会拉入 settings 等 UI 模块)。两个组件各自把本片段
拼到自己的 ES module 前面, 再调用 ``_ctxKit()`` 得到独立实例。
"""

CTX_MENU_KIT_JS = r"""
function _ctxKit() {
  var toastTimer = null;

  function make(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }

  function icon(paths, name) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true">' + (paths[name] || '') + '</svg>';
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

  function copy(text, notify) {
    if (!text) { return; }
    // 先给反馈再写剪贴板: 部分浏览器 clipboard Promise 长时间不落定,
    // 等它再提示会让用户以为没生效。
    if (notify) { notify('已复制到剪贴板'); }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).catch(function () {
        fallbackCopy(text);
      });
    } else {
      fallbackCopy(text);
    }
  }

  function toast(text, id) {
    var el = document.getElementById(id);
    if (!el) {
      el = make('div', 'fp-ctx-toast');
      el.id = id;
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

  function place(box, x, y) {
    // 先按左上角定位量出尺寸, 再贴边翻转, 避免菜单溢出窗口。
    // clientWidth/Height 不含滚动条; innerWidth 会把菜单贴到 Windows
    // 经典滚动条下面。
    box.style.left = '0px';
    box.style.top = '0px';
    box.style.visibility = 'hidden';
    box.classList.add('fp-ctx-open');
    var r = box.getBoundingClientRect();
    var vw = document.documentElement.clientWidth || window.innerWidth;
    var vh = document.documentElement.clientHeight || window.innerHeight;
    var left = Math.max(8, Math.min(x, vw - r.width - 8));
    var top = y + r.height > vh - 8 ? y - r.height : y;
    top = Math.max(8, Math.min(top, vh - r.height - 8));
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.style.visibility = 'visible';
  }

  return {
    make: make,
    icon: icon,
    copy: copy,
    toast: toast,
    place: place,
  };
}
"""
