
export default function (component) {
  var K = _ctxKit();
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
  // 触控板「两指双击」会在同一位置连发两次 contextmenu。第一次已经打开了
  // 菜单, 第二次如果当作新的右键处理, 轻则菜单闪一下重开, 重则落在刚弹出的
  // 菜单上触发浏览器原生菜单。这里把短时间同位置的重复右键视为同一次手势。
  var lastContextAt = 0;
  var lastContextX = 0;
  var lastContextY = 0;

  function ensureMenu() {
    if (menu && document.body.contains(menu)) { return menu; }
    menu = document.createElement('div');
    menu.id = MENU_ID;
    menu.className = 'fp-ctx-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', '右键菜单');
    menu.tabIndex = -1;
    // 右键落在菜单自身时保留自定义菜单, 不要叠出浏览器原生菜单
    // (两指双击的第二次事件常常正好落在刚弹出的菜单上)。
    menu.addEventListener('contextmenu', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    });
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
  function hideAllMenus() {
    // 图表菜单由 charting.py 创建, 不在本组件的 menu 变量里; 开始新的右键
    // 手势时一并收起, 避免原生菜单和旧的自定义菜单同时留在页面上。
    hideMenu();
    var others = document.querySelectorAll('.fp-ctx-menu');
    for (var i = 0; i < others.length; i++) { others[i].remove(); }
  }

  function toast(text) {
    K.toast(text, TOAST_ID);
  }

  function copy(text) {
    K.copy(text, toast);
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
    return K.icon(paths, name);
  }

  function showMenu(x, y, title, items) {
    var box = ensureMenu();
    box.innerHTML = '';
    if (title) {
      box.appendChild(K.make('div', 'fp-ctx-header', title));
    }
    items.forEach(function (item) {
      if (item.sep) {
        box.appendChild(K.make('div', 'fp-ctx-sep'));
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
    box.setAttribute('aria-hidden', 'false');
    K.place(box, x, y);
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
      // 跳过 0×0 的无障碍单元格 (Chromium/Edge), 否则它们会在 (0,0)
      // 误命中页面左上角的右键
      if (r.width <= 0 || r.height <= 0) { continue; }
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        return cells[i];
      }
      // 行右侧空白/行号列也可能落点: 同一行内取水平距离最近的单元格,
      // 这样右键整行任意位置都能拿到 symbol, 不会掉回浏览器默认菜单。
      // 注意: Chromium/Edge 的 Glide 用 canvas 绘制单元格, 无障碍表格里的
      // td 全是 0×0, 这个分支找不到任何单元格, 需要走下面的 canvas 几何。
      if (y >= r.top && y <= r.bottom) {
        var d = x < r.left ? r.left - x : (x > r.right ? x - r.right : 0);
        if (d < rowDist) { rowDist = d; rowBest = cells[i]; }
      }
    }
    return rowBest || cellFromCanvas(df, x, y);
  }

  // Chromium/Edge 下 st.dataframe 由 Glide 的 canvas 渲染, 可访问性表格里的
  // 单元格没有尺寸/坐标。这里从 canvas 节点上的 React props 取出列宽、行高和
  // 滚动偏移, 把鼠标坐标换算成列/行, 再回查同位置的无障碍 td 取文本。
  function gridProps(el) {
    var keys = Object.keys(el);
    var fiber = null;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactFiber$') === 0) { fiber = el[keys[i]]; break; }
    }
    for (var guard = 0; fiber && guard < 40; guard++, fiber = fiber.return) {
      var p = fiber.memoizedProps;
      if (p && Array.isArray(p.columns) && typeof p.rows === 'number' &&
          typeof p.rowHeight === 'number' && p.getCellContent) {
        return p;
      }
    }
    return null;
  }

  function columnAt(props, relX, scrollLeft) {
    var cols = props.columns;
    if (!cols || !cols.length) { return null; }
    var widths = [];
    for (var i = 0; i < cols.length; i++) {
      var w = Number(cols[i].width);
      widths.push(w > 0 ? w : 0);
    }
    var freeze = Math.max(
      0, Math.min(Number(props.freezeColumns) || 0, cols.length)
    );
    var frozenWidth = 0;
    for (i = 0; i < freeze; i++) { frozenWidth += widths[i]; }
    if (freeze > 0 && relX < frozenWidth) {
      var acc = 0;
      for (i = 0; i < freeze; i++) {
        acc += widths[i];
        if (relX < acc) { return i; }
      }
      return freeze - 1;
    }
    // 冻结列固定在左侧; 其余列按 scrollLeft 平移.
    var contentX = (relX - frozenWidth) + scrollLeft;
    var acc2 = 0;
    for (i = freeze; i < cols.length; i++) {
      acc2 += widths[i];
      if (contentX < acc2) { return i; }
    }
    return null;
  }

  function isDataColumn(meta) {
    return !(meta && meta.id == null && !meta.title);
  }

  function nearestDataColumn(props, col) {
    var cols = props.columns || [];
    // 落在行号/选择列或最后一列右侧空白: 与 Firefox 的 DOM 命中一致,
    // 取同一行最近的数据列, 不把用户打回浏览器原生菜单.
    if (col == null) {
      for (var i = cols.length - 1; i >= 0; i--) {
        if (isDataColumn(cols[i])) { return i; }
      }
      return null;
    }
    if (col >= 0 && col < cols.length && isDataColumn(cols[col])) {
      return col;
    }
    for (var j = col + 1; j < cols.length; j++) {
      if (isDataColumn(cols[j])) { return j; }
    }
    for (var k = col - 1; k >= 0; k--) {
      if (isDataColumn(cols[k])) { return k; }
    }
    return null;
  }

  function cellFromCanvas(df, x, y) {
    var canvas = df.querySelector('[data-testid="data-grid-canvas"]');
    if (!canvas) { return null; }
    var rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) { return null; }
    if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) {
      return null;
    }
    var props = gridProps(canvas);
    if (!props) { return null; }
    var header = Number(props.headerHeight) || 0;
    var rowHeight = Number(props.rowHeight) || 0;
    if (rowHeight <= 0) { return null; }
    var relY = y - rect.top;
    if (relY < header) { return null; }
    var rowOffset = Number(props.cellYOffset);
    if (!isFinite(rowOffset)) { rowOffset = 0; }
    var row = rowOffset + Math.floor((relY - header) / rowHeight);
    if (row < 0 || row >= props.rows) { return null; }
    var scroller = df.querySelector('.dvn-scroller');
    var scrollLeft = scroller ? (scroller.scrollLeft || 0) : 0;
    var col = nearestDataColumn(
      props, columnAt(props, x - rect.left, scrollLeft)
    );
    if (col == null) { return null; }
    // 目标列可能被横向虚拟化出无障碍表格; 向左/右回退到同屏最近的数据列
    function find(colIndex) {
      if (colIndex < 0 || colIndex >= props.columns.length) { return null; }
      if (!isDataColumn(props.columns[colIndex])) { return null; }
      return df.querySelector(
        '[data-testid="glide-cell-' + colIndex + '-' + row + '"]'
      );
    }
    for (var left = col; left >= 0; left--) {
      var hit = find(left);
      if (hit) { return hit; }
    }
    for (var right = col + 1; right < props.columns.length; right++) {
      var hit2 = find(right);
      if (hit2) { return hit2; }
    }
    return null;
  }

  function cellParts(cell) {
    var m = /^glide-cell-(\d+)-(\d+)$/.exec(cell.getAttribute('data-testid') || '');
    return m ? { col: parseInt(m[1], 10), row: parseInt(m[2], 10) } : null;
  }

  function gridCellText(content) {
    if (!content) { return ''; }
    var copyData = content.copyData;
    if (typeof copyData === 'string' || typeof copyData === 'number' ||
        typeof copyData === 'boolean') {
      return String(copyData);
    }
    if (typeof content.displayData === 'string') { return content.displayData; }
    if (copyData && typeof copyData.value !== 'undefined') {
      return String(copyData.value);
    }
    if (typeof content.data === 'string' || typeof content.data === 'number') {
      return String(content.data);
    }
    if (content.data && typeof content.data.value !== 'undefined') {
      return String(content.data.value);
    }
    return '';
  }

  // 通过 canvas 的 React props 读取整行: 可见列之外的数据也能复制到,
  // 不依赖无障碍表格里是否存在该列的 td (Chromium/Edge 只保留可见列)。
  function rowFromGrid(df, row) {
    var canvas = df.querySelector('[data-testid="data-grid-canvas"]');
    if (!canvas) { return null; }
    var props = gridProps(canvas);
    if (!props || row < 0 || row >= props.rows) { return null; }
    var values = [];
    var headers = [];
    var symbol = '';
    var hasSymbolCol = false;
    for (var col = 0; col < props.columns.length; col++) {
      var meta = props.columns[col];
      // 行选择/序号列没有 id 也没有标题, 不属于数据列
      if (meta.id == null && !meta.title) { continue; }
      var title = String(meta.title || '');
      var content = null;
      try { content = props.getCellContent([col, row]); } catch (e) {}
      var text = gridCellText(content);
      values.push({ col: col, text: text });
      headers.push(title);
      if (title === '代码' || title.toLowerCase() === 'symbol') {
        hasSymbolCol = true;
        if (text) { symbol = text; }
      }
    }
    if (!values.length) { return null; }
    // 有代码列却取不到代码时退回无障碍表格, 避免 K线/删除入口凭空消失
    if (hasSymbolCol && !symbol) { return null; }
    return { values: values, headers: headers, symbol: symbol };
  }

  function rowInfo(df, pos) {
    var fromGrid = rowFromGrid(df, pos.row);
    if (fromGrid) { return fromGrid; }
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
    // 右键落在自定义菜单上: 吞掉事件, 否则浏览器原生菜单会叠上来
    if (t.closest('.fp-ctx-menu, .fp-ctx-toast')) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    var now = Date.now();
    // 触控板两指双击: 第二次 contextmenu 属于同一次手势, 保留已打开的菜单。
    // 不做这层去重时, 第二次事件可能落在刚弹出的菜单上并触发原生菜单。
    // 这里检查任意 .fp-ctx-menu (图表菜单由 charting.py 创建, 不在本组件的
    // menu 变量里), 否则图表的第二下仍会漏出浏览器原生菜单。
    // 阈值覆盖系统「双击速度」的慢速设置 (macOS 可调到 ~900ms): 同一位置
    // 的第二次右键最多只是把已打开的菜单保留下来, 不会误伤正常的再次右键。
    var openMenu = menu || document.querySelector('.fp-ctx-menu');
    if (openMenu && now - lastContextAt < 900 &&
        Math.abs(ev.clientX - lastContextX) <= 10 &&
        Math.abs(ev.clientY - lastContextY) <= 10) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    lastContextAt = now;
    lastContextX = ev.clientX;
    lastContextY = ev.clientY;
    // 新的右键开始: 先收起旧菜单 (输入框/正文仍交给浏览器原生菜单)
    hideAllMenus();
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
            // 用相对 URL: 反向代理/--server.baseUrlPath 部署时 /kline 的
            // 根路径不存在, 必须保留当前挂载前缀.
            window.location.assign(
              new URL(
                'kline?symbol=' + encodeURIComponent(row.symbol) +
                '&t=' + Date.now(),
                window.location.href
              ).toString()
            );
          }
        });
        // 只对只读明细表提供删除: 编辑器的未保存行不在磁盘上, 右键删除
        // 会清空整个编辑器状态却提示「已删除」, 还可能删错已保存的旧代码。
        if (tableKey === 'watchlist_detail_select') {
          items.push({
            label: '从自选删除',
            icon: 'trash',
            action: function () {
              sendContextAction('delete_watchlist', row.symbol);
            }
          });
        } else if (tableKey === 'holdings_detail_select') {
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
    if (!menu) { return; }
    if (menu.contains(ev.target)) { return; }
    // 右键/Control+左键的 pointerdown 先于 contextmenu: 不能在 contextmenu
    // 到达前把菜单关掉, 否则两指双击的第二下会先关后开 (图表菜单还会漏出
    // 浏览器原生菜单)。右键的收起/重开统一交给 onContext。
    if (ev.button === 2 || ev.ctrlKey) { return; }
    hideMenu();
  }
  function onFocusIn(ev) {
    // Tab 到菜单外时收起, 避免键盘焦点走了菜单还浮在页面上
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
  document.addEventListener('focusin', onFocusIn, true);
  window.addEventListener('scroll', onViewportChange, true);
  window.addEventListener('resize', onViewportChange);

  var cleanup = function () {
    document.removeEventListener('contextmenu', onContext, true);
    document.removeEventListener('pointerdown', onPointerDown, true);
    document.removeEventListener('keydown', onKeyDown, true);
    document.removeEventListener('focusin', onFocusIn, true);
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
