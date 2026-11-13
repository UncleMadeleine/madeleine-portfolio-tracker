
export default function (component) {
  var K = _ctxKit();
  var CFG = typeof component.data === 'string'
    ? JSON.parse(component.data) : component.data;
  var root = component.parentElement;
  var el = root.querySelector('.lwc-chart');
  var legend = root.querySelector('.lwc-legend');
  // data 变化时 Streamlit 只重执行、不先调用上一次 cleanup: 先手动清掉上一轮
  // 挂在 document/window 上的右键监听器, 否则每次重绘都会多叠一层。
  if (root.__fpChartDispose) {
    try { root.__fpChartDispose(); } catch (e) {}
    root.__fpChartDispose = null;
  }
  // data 变化时 Streamlit 会重新执行本函数但不调用上一次的 cleanup (仅 unmount 时调用),
  // 残留的旧 chart 实例会叠层并遮挡新图 — 挂载前先清空容器。
  while (el.firstChild) { el.removeChild(el.firstChild); }
  if (CFG.height) { el.parentElement.style.height = CFG.height + 'px'; }
  var LC = window.LightweightCharts;
  var chart = LC.createChart(el, CFG.options);
  var isCompare = CFG.mode === 'compare';
  var isCrypto = CFG.mode === 'crypto';
  var candle = null, volSeries = null, tracks = [], indSeries = [], ratioSeries = null;
  var ratio = CFG.ratio || null;

  function addLine(color, data) {
    var s = chart.addSeries(LC.LineSeries, {
      color: color, lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: true, crosshairMarkerRadius: 3,
      priceFormat: { type: 'price', precision: CFG.precision, minMove: CFG.minMove }
    });
    s.setData(data);
    return s;
  }
  if (isCompare) {
    tracks = CFG.lines.map(function (ln) {
      return { name: ln.name, color: ln.color, series: addLine(ln.color, ln.data), data: ln.data };
    });
    // 可选比例线: 与对比折线同图, 但使用独立的左侧价轴 (左侧价轴默认隐藏,
    // 这里显式启用); 画的是两标的的「具体比值」(不归一化), 虚线为区间均值。
    if (ratio) {
      ratioSeries = chart.addSeries(LC.LineSeries, {
        color: ratio.color || '#ff5da2', lineWidth: 2,
        priceScaleId: 'left',
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: true, crosshairMarkerRadius: 3,
        priceFormat: { type: 'price', precision: ratio.precision, minMove: Math.pow(10, -ratio.precision) }
      });
      ratioSeries.setData(ratio.data);
      if (ratio.mean != null) {
        ratioSeries.createPriceLine({
          price: ratio.mean, color: '#8b949e', lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: '均值'
        });
      }
      try {
        chart.applyOptions({ leftPriceScale: { visible: true } });
        chart.priceScale('left').applyOptions({
          scaleMargins: { top: 0.1, bottom: 0.1 },
          borderColor: 'rgba(120,140,180,0.25)'
        });
      } catch (e) {}
    }
    // 水平参考线 (单序列折线用): 如 AH 溢价率 0% 同价线, 不占用独立价轴
    if (CFG.reference != null && tracks.length) {
      tracks[0].series.createPriceLine({
        price: CFG.reference, color: '#8b949e', lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: CFG.referenceLabel || ''
      });
    }
  } else if (isCrypto) {
    // 加密货币对比: 每币一个独立窗格的蜡烛图 (原始价格不归一化, 各自价轴,
    // 共享时间轴与十字光标); 窗格分隔线可拖拽调高。
    tracks = CFG.coins.map(function (cn, paneIdx) {
      var s = chart.addSeries(LC.CandlestickSeries, {
        upColor: CFG.up, downColor: CFG.down,
        wickUpColor: CFG.up, wickDownColor: CFG.down,
        borderVisible: false,
        priceLineVisible: true, priceLineStyle: 2,
        priceLineColor: cn.color,
        priceFormat: { type: 'price', precision: cn.precision, minMove: cn.minMove }
      }, paneIdx);
      s.setData(cn.candles);
      return { name: cn.name, color: cn.color, series: s, data: cn.candles, precision: cn.precision };
    });
  } else {
    candle = chart.addSeries(LC.CandlestickSeries, CFG.candleOpts);
    candle.setData(CFG.candles);
    tracks = CFG.mas.map(function (m) {
      return { name: m.name, color: m.color, series: addLine(m.color, m.data), data: m.data };
    });
    if (CFG.volume.length) {
      volSeries = chart.addSeries(LC.HistogramSeries, {
        priceFormat: { type: 'volume' }, priceScaleId: '',
        priceLineVisible: false, lastValueVisible: false
      }, 1);
      volSeries.setData(CFG.volume);
      try {
        var panes = chart.panes();
        if (panes.length > 1) { panes[1].setStretchFactor(0.32); }
      } catch (e) {}
    }

    // 技术指标: 布林带叠加主图, MACD/RSI/KDJ 各占独立副图
    if (CFG.boll && CFG.boll.length) {
      CFG.boll.forEach(function (b) {
        tracks.push({ name: b.name, color: b.color, series: addLine(b.color, b.data), data: b.data });
      });
    }
    var indPane = CFG.volume.length ? 2 : 1;
    function addIndLine(color, data, paneIdx) {
      var s = chart.addSeries(LC.LineSeries, {
        color: color, lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: true, crosshairMarkerRadius: 2,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 }
      }, paneIdx);
      s.setData(data);
      return s;
    }
    if (CFG.macd) {
      var mp = indPane++;
      var difS = addIndLine(CFG.macd.difColor, CFG.macd.dif, mp);
      var deaS = addIndLine(CFG.macd.deaColor, CFG.macd.dea, mp);
      var histS = chart.addSeries(LC.HistogramSeries, {
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: false, lastValueVisible: false
      }, mp);
      histS.setData(CFG.macd.hist);
      indSeries.push({ name: 'DIF', color: CFG.macd.difColor, series: difS, data: CFG.macd.dif });
      indSeries.push({ name: 'DEA', color: CFG.macd.deaColor, series: deaS, data: CFG.macd.dea });
    }
    if (CFG.rsi) {
      var rp = indPane++;
      var rsiS = addIndLine(CFG.rsi.color, CFG.rsi.data, rp);
      indSeries.push({ name: 'RSI', color: CFG.rsi.color, series: rsiS, data: CFG.rsi.data });
    }
    if (CFG.kdj) {
      var kp = indPane++;
      var kdjKeys = [['k', 'K', CFG.kdj.kColor], ['d', 'D', CFG.kdj.dColor], ['j', 'J', CFG.kdj.jColor]];
      kdjKeys.forEach(function (kk) {
        var s = addIndLine(kk[2], CFG.kdj[kk[0]], kp);
        indSeries.push({ name: kk[1], color: kk[2], series: s, data: CFG.kdj[kk[0]] });
      });
    }
    // 调整副图高度比例: 主图大, 指标副图小
    try {
      var allPanes = chart.panes();
      if (allPanes.length > 1) {
        allPanes[0].setStretchFactor(3);
        for (var pi = 1; pi < allPanes.length; pi++) {
          allPanes[pi].setStretchFactor(0.5);
        }
      }
    } catch (e) {}
  }

  var timeIdx = {};
  if (!isCompare) {
    CFG.candles.forEach(function (b, i) { timeIdx[b.time] = i; });
  }
  function fp(v) {
    return v == null ? '—' : Number(v).toFixed(CFG.precision) + (CFG.unit || '');
  }
  function fp2(v) { return v == null ? '—' : Number(v).toFixed(2); }
  function fv(v) {
    if (v == null) return '—';
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return Math.round(v).toString();
  }
  // 图例用 innerHTML 拼接 (需要分段着色), 所有来自 payload/代码名的文本
  // 必须先转义 —— 否则自选/数据源返回的 name 含 HTML 时会在页面里执行。
  function escHtml(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function span(txt, color) {
    return '<span style="color:' + escHtml(color || '#c9d1d9') + '">' + escHtml(txt) + '</span>';
  }
  function tstr(t) {
    if (t == null) return '';
    if (typeof t === 'string') return t;
    if (typeof t === 'object' && t.year) {
      return t.year + '-' + ('0' + t.month).slice(-2) + '-' + ('0' + t.day).slice(-2);
    }
    return String(t);
  }
  // 各市场交易日历不同: 按时间二分查找该系列 ≤ t 的最近取值
  function valAt(tr, t) {
    var lo = 0, hi = tr.data.length - 1, ans = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (tr.data[mid].time <= t) { ans = tr.data[mid].value; lo = mid + 1; }
      else { hi = mid - 1; }
    }
    return ans;
  }
  // 每币窗格蜡烛定位: 二分查找 ≤ t 的最近 bar 下标 (无则 -1)
  function barIdx(data, t) {
    var lo = 0, hi = data.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (data[mid].time <= t) { ans = mid; lo = mid + 1; }
      else { hi = mid - 1; }
    }
    return ans;
  }
  function fpn(v, p) { return v == null ? '—' : Number(v).toFixed(p); }
  function show(param) {
    var h, i;
    if (isCompare) {
      var t = '';
      tracks.forEach(function (tr) {
        var lt = tr.data[tr.data.length - 1].time;
        if (lt > t) { t = lt; }
      });
      if (param && param.time) { t = tstr(param.time); }
      h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(t, '#8b949e') + '&nbsp;&nbsp;';
      tracks.forEach(function (tr) {
        var v = valAt(tr, t);
        var first = tr.data.length ? tr.data[0].value : null;
        var pct = (v != null && first) ? (v / first - 1) * 100 : null;
        h += ' ' + span(tr.name, tr.color) + ' ' + span(fp(v));
        if (pct != null && CFG.showChange !== false) {
          h += ' ' + span('(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)',
            pct >= 0 ? CFG.up : CFG.down);
        }
        h += '&nbsp;&nbsp;';
      });
      if (ratio && ratioSeries) {
        // 比例线信息单独一行, 避免多代码时被标题/涨跌信息挤出可视区
        h += '<br>';
        var rv = valAt({ data: ratio.data }, t);
        h += ' ' + span('比例 ' + ratio.name, ratio.color) + ' ' + span(fpn(rv, ratio.precision));
        if (rv != null && ratio.mean) {
          var dev = (rv / ratio.mean - 1) * 100;
          h += ' ' + span('(均值 ' + fpn(ratio.mean, ratio.precision) + ', 较均值 '
            + (dev >= 0 ? '+' : '') + dev.toFixed(2) + '%)', '#8b949e');
        }
        h += '&nbsp;&nbsp;';
      }
      legend.innerHTML = h;
      return;
    }
    if (isCrypto) {
      var ct = '';
      tracks.forEach(function (tr) {
        var lt = tr.data.length ? tr.data[tr.data.length - 1].time : '';
        if (lt > ct) { ct = lt; }
      });
      if (param && param.time) { ct = tstr(param.time); }
      h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(ct, '#8b949e') + '&nbsp;&nbsp;';
      tracks.forEach(function (tr) {
        var bi = barIdx(tr.data, ct);
        if (bi < 0) {
          h += ' ' + span(tr.name, tr.color) + ' ' + span('—') + '&nbsp;&nbsp;';
          return;
        }
        var cb = tr.data[bi], cp = tr.precision;
        var cprev = bi > 0 ? tr.data[bi - 1].close : cb.open;
        var cpct = cprev ? (cb.close / cprev - 1) * 100 : 0;
        var cdir = cb.close >= cb.open ? CFG.up : CFG.down;
        h += ' ' + span(tr.name, tr.color)
          + ' 开' + span(fpn(cb.open, cp)) + ' 高' + span(fpn(cb.high, cp), cdir)
          + ' 低' + span(fpn(cb.low, cp), cdir) + ' 收' + span(fpn(cb.close, cp), cdir)
          + ' ' + span((cpct >= 0 ? '+' : '') + cpct.toFixed(2) + '%',
            cpct >= 0 ? CFG.up : CFG.down)
          + '&nbsp;&nbsp;';
      });
      legend.innerHTML = h;
      return;
    }
    var bar = null, vol = null, maVals = null, indVals = null;
    i = CFG.candles.length - 1;
    if (param && param.seriesData) {
      var d = param.seriesData.get(candle);
      if (d && typeof d.open === 'number') { bar = d; i = timeIdx[d.time]; }
      if (volSeries) { var v = param.seriesData.get(volSeries); vol = v ? v.value : null; }
      maVals = tracks.map(function (m) {
        var mv = param.seriesData.get(m.series);
        return { name: m.name, color: m.color, value: mv ? mv.value : null };
      });
      indVals = indSeries.map(function (t) {
        var mv = param.seriesData.get(t.series);
        return { name: t.name, color: t.color, value: mv ? mv.value : null };
      });
    }
    if (!bar) {
      bar = CFG.candles[i];
      vol = CFG.volume.length ? CFG.volume[i].value : null;
      maVals = tracks.map(function (m) {
        return { name: m.name, color: m.color, value: m.data[m.data.length - 1].value };
      });
      indVals = indSeries.map(function (t) {
        return { name: t.name, color: t.color, value: t.data.length ? t.data[t.data.length - 1].value : null };
      });
    }
    var prev = i > 0 ? CFG.candles[i - 1].close : bar.open;
    var pct = prev ? (bar.close / prev - 1) * 100 : 0;
    var dir = bar.close >= bar.open ? CFG.up : CFG.down;
    var pctColor = pct >= 0 ? CFG.up : CFG.down;
    h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(bar.time, '#8b949e') + '&nbsp;&nbsp;'
      + '开' + span(fp(bar.open)) + ' 高' + span(fp(bar.high), dir)
      + ' 低' + span(fp(bar.low), dir) + ' 收' + span(fp(bar.close), dir)
      + ' ' + span((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%', pctColor);
    if (vol != null) { h += ' 量' + span(fv(vol)); }
    maVals.forEach(function (m) { h += ' ' + span(m.name + ' ' + fp(m.value), m.color); });
    if (indVals) { indVals.forEach(function (t) { h += ' ' + span(t.name + ' ' + fp2(t.value), t.color); }); }
    legend.innerHTML = h;
  }
  chart.subscribeCrosshairMove(show);
  show(null);

  var n = isCompare
    ? Math.max.apply(null, CFG.lines.map(function (l) { return l.data.length; })
        .concat([ratio ? ratio.data.length : 1, 1]))
    : isCrypto
      ? Math.max.apply(null, CFG.coins.map(function (cn) { return cn.candles.length; }).concat([1]))
      : CFG.candles.length;
  // 视口锚点的索引空间: 单只K线用蜡烛数组; 对比/加密货币对比用全系列时间轴
  // (payload 的 axis = 各系列时间的并集, 升序去重)。这两种模式 candles 恒为
  // 空数组, 不换锚点空间的话锚点永远不写不读, 数据更新后拖动位置必然重置
  var bars = CFG.candles.length ? CFG.candles
    : (CFG.axis || []).map(function (t) { return { time: t }; });
  // 视口还原: 组件因数据更新重建时, 按「锚点 bar 时间 + 分数偏移 + 半宽」还原拖动位置
  // (bar 索引无关, 日/周/月K 通用)。锚点存 window 级 store (按 queryId 键):
  // Streamlit 更新 data 时会重建 .lwc-wrap DOM 节点, DOM 属性随之丢失;
  // queryId 变化 (换代码) 时旧键自然失效回退初始区间。
  if (!window.__lwcViewStore) { window.__lwcViewStore = {}; }
  var viewStore = CFG.queryId ? window.__lwcViewStore[CFG.queryId] : null;
  var restored = null;
  if (viewStore && viewStore.t && bars.length) {
    var vlo = 0, vhi = bars.length - 1, vai = -1;
    while (vlo <= vhi) {
      var vmi = (vlo + vhi) >> 1;
      if (bars[vmi].time === viewStore.t) { vai = vmi; break; }
      if (bars[vmi].time < viewStore.t) { vlo = vmi + 1; } else { vhi = vmi - 1; }
    }
    if (vai >= 0) {
      restored = {
        from: vai + viewStore.frac - viewStore.half,
        to: vai + viewStore.frac + viewStore.half,
      };
    }
  }
  try {
    chart.timeScale().setVisibleLogicalRange(
      restored || { from: n - Math.min(n, CFG.initBars), to: n - 1 + 4 }
    );
  } catch (e) {
    try { chart.timeScale().fitContent(); } catch (e2) {}
  }
  // 每次视口变化先写锚点 (供数据更新后还原拖动位置)
  chart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
    if (!range) return;
    if (!bars.length) return;
    var mid = (range.from + range.to) / 2;
    var fl = Math.max(0, Math.min(bars.length - 1, Math.floor(mid)));
    window.__lwcViewStore[CFG.queryId] = {
      t: bars[fl].time,
      frac: mid - fl,
      half: (range.to - range.from) / 2,
    };
  });

  // ---------- 触控板双指捏合缩放 ----------
  // Chrome/Edge/Firefox 把触控板捏合映射成带 ctrlKey 的 wheel, 但
  // lightweight-charts 的缩放步长按鼠标滚轮 (deltaY≈±100) 标定: 触控板每帧
  // deltaY 只有 ±1~5, Windows 上还要再除以 devicePixelRatio, 结果几乎不动,
  // 同时浏览器会先执行整页缩放。Safari 则根本不发 ctrl+wheel, 只发私有的
  // gesturestart/gesturechange/gestureend。这里统一接管这两条路径: 阻止
  // 浏览器整页缩放, 以光标为锚点按连续倍率缩放时间轴 (与 TradingView 一致)。
  var PINCH_WHEEL_RATE = 0.01;
  function zoomTimeScaleAt(clientX, factor) {
    if (!(factor > 0) || factor === 1) { return; }
    var ts = chart.timeScale();
    var range = ts.getVisibleLogicalRange();
    if (!range) { return; }
    var rect = el.getBoundingClientRect();
    // Safari 的 GestureEvent 不一定带 clientX: 拿不到就以视口中心为锚点
    var localX = typeof clientX === 'number' ? clientX - rect.left : NaN;
    var anchor = isFinite(localX) ? ts.coordinateToLogical(localX) : null;
    if (anchor == null || !isFinite(anchor)) {
      anchor = (range.from + range.to) / 2;
    }
    anchor = Math.max(range.from, Math.min(range.to, anchor));
    var from = anchor - (anchor - range.from) / factor;
    var to = anchor + (range.to - anchor) / factor;
    // 最多放大到 2 根 bar 宽, 再窄会让十字光标/蜡烛渲染失去意义
    if (to - from < 2) {
      var mid = (from + to) / 2;
      from = mid - 1;
      to = mid + 1;
    }
    try {
      ts.setVisibleLogicalRange({ from: from, to: to });
    } catch (e) {}
  }
  function onPinchWheel(ev) {
    if (!ev.ctrlKey) { return; }
    // 触摸屏双指由 lightweight-charts 的 handleScale.pinch 处理; wheel 分支
    // 只对应触控板/按住 Ctrl 的滚轮缩放。
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.stopImmediatePropagation) { ev.stopImmediatePropagation(); }
    var dy = ev.deltaY || 0;
    if (ev.deltaMode === 1) { dy *= 16; }
    else if (ev.deltaMode === 2) { dy *= 100; }
    // 单次事件的缩放幅度封顶, 避免 Ctrl+鼠标滚轮一格直接放大/缩小一个量级
    dy = Math.max(-20, Math.min(20, dy));
    if (dy) { zoomTimeScaleAt(ev.clientX, Math.exp(-dy * PINCH_WHEEL_RATE)); }
  }
  var gestureScale = 1;
  function onGestureStart(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    gestureScale = ev.scale || 1;
  }
  function onGestureChange(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    var s = ev.scale || gestureScale;
    var factor = gestureScale ? s / gestureScale : 1;
    gestureScale = s;
    zoomTimeScaleAt(ev.clientX, factor);
  }
  function onGestureEnd(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    gestureScale = 1;
  }
  el.addEventListener('wheel', onPinchWheel, { capture: true, passive: false });
  el.addEventListener('gesturestart', onGestureStart, { passive: false });
  el.addEventListener('gesturechange', onGestureChange, { passive: false });
  el.addEventListener('gestureend', onGestureEnd, { passive: false });

  // ---------- 图表右键菜单 ----------
  // 券商/交易软件里右键图表通常给「重置缩放 / 复制 / 导出」, 而不是浏览器
  // 原生菜单。菜单挂到 document.body 上, 复用 theme.py 的全局 .fp-ctx-* 样式;
  // 图表在 ShadowRoot 里, 但组件 JS 不受 sandbox 限制, 可以直接操作宿主 DOM。
  var ctxEl = null;
  // 触控板两指双击会在同一位置连发两次 contextmenu, 第二次不能把刚弹出的
  // 菜单当成新的右键: 否则菜单会闪一下重开, 甚至叠出浏览器原生菜单。
  var ctxLastAt = 0;
  var ctxLastX = 0;
  var ctxLastY = 0;
  function cxHide() {
    if (ctxEl) {
      // 同 theme.py: 移除节点, 避免反复右键在 body 里堆积空菜单
      ctxEl.remove();
      ctxEl = null;
    }
  }
  function cxToast(text) {
    K.toast(text, 'fp-context-toast');
  }
  function cxCopy(text) {
    K.copy(text, cxToast);
  }
  function cxDownload(blob, filename) {
    if (!blob) { return; }
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }
  function cxCsvCell(v) {
    var s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function cxCsv() {
    var rows = [];
    function add(values) {
      rows.push(values.map(cxCsvCell).join(','));
    }
    if (isCompare) {
      add(['series', 'time', 'value']);
      tracks.forEach(function (tr) {
        tr.data.forEach(function (p) { add([tr.name, tstr(p.time), p.value]); });
      });
      if (ratio) {
        ratio.data.forEach(function (p) { add(['ratio', tstr(p.time), p.value]); });
      }
    } else if (isCrypto) {
      add(['symbol', 'time', 'open', 'high', 'low', 'close']);
      tracks.forEach(function (tr) {
        tr.data.forEach(function (b) {
          add([tr.name, tstr(b.time), b.open, b.high, b.low, b.close]);
        });
      });
    } else {
      add(['time', 'open', 'high', 'low', 'close', 'volume']);
      CFG.candles.forEach(function (b, i) {
        add([tstr(b.time), b.open, b.high, b.low, b.close,
          CFG.volume[i] ? CFG.volume[i].value : '']);
      });
    }
    return rows.join('\n');
  }
  function cxFilename(ext) {
    var base = String(CFG.copyText || CFG.title || 'chart')
      .replace(/[^\w\u4e00-\u9fa5.\-]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'chart';
    return base + '.' + ext;
  }
  function cxReset() {
    try {
      if (window.__lwcViewStore) { window.__lwcViewStore[CFG.queryId] = null; }
      chart.timeScale().setVisibleLogicalRange(
        { from: n - Math.min(n, CFG.initBars), to: n - 1 + 4 }
      );
    } catch (e) {
      try { chart.timeScale().fitContent(); } catch (e2) {}
    }
  }
  function cxIcon(name) {
    var paths = {
      reset: '<path d="M3 12a9 9 0 1 0 3-6.7"></path><path d="M3 4v5h5"></path>',
      copy: '<rect x="9" y="9" width="11" height="11" rx="2"></rect>' +
        '<path d="M5 15V5a2 2 0 0 1 2-2h10"></path>',
      table: '<rect x="3" y="4" width="18" height="16" rx="2"></rect>' +
        '<path d="M3 10h18M9 4v16"></path>',
      download: '<path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path>' +
        '<path d="M5 21h14"></path>'
    };
    return K.icon(paths, name);
  }
  function cxShow(x, y) {
    if (ctxEl) { cxHide(); }
    ctxEl = document.createElement('div');
    ctxEl.className = 'fp-ctx-menu';
    ctxEl.setAttribute('role', 'menu');
    ctxEl.setAttribute('aria-label', '图表右键菜单');
    ctxEl.tabIndex = -1;
    // 右键落在菜单自身时保留自定义菜单, 不叠浏览器原生菜单
    ctxEl.addEventListener('contextmenu', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
    });

    var head = K.make('div', 'fp-ctx-header', CFG.copyText || CFG.title || '图表');
    ctxEl.appendChild(head);

    var items = [
      { label: '重置缩放', icon: 'reset', action: cxReset },
      { label: '复制代码', icon: 'copy',
        action: function () { cxCopy(CFG.copyText || CFG.title || ''); } },
      { label: '复制数据 (CSV)', icon: 'table',
        action: function () { cxCopy(cxCsv()); } },
      { sep: true },
      { label: '导出图片 (PNG)', icon: 'download', action: function () {
        try {
          var canvas = chart.takeScreenshot();
          if (canvas && canvas.toBlob) {
            canvas.toBlob(function (blob) {
              cxDownload(blob, cxFilename('png'));
            }, 'image/png');
          }
        } catch (e) {}
      } }
    ];
    items.forEach(function (item) {
      if (item.sep) {
        ctxEl.appendChild(K.make('div', 'fp-ctx-sep'));
        return;
      }
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fp-ctx-item';
      btn.setAttribute('role', 'menuitem');
      btn.innerHTML = cxIcon(item.icon) + '<span></span>';
      btn.querySelector('span').textContent = item.label;
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        cxHide();
        item.action();
      });
      ctxEl.appendChild(btn);
    });

    document.body.appendChild(ctxEl);
    K.place(ctxEl, x, y);
    // 鼠标右键打开时不预选第一项; 键盘用户按 ArrowDown 进入菜单项。
  }
  function cxOnContext(ev) {
    var now = Date.now();
    // 触控板两指双击: 第二次 contextmenu 是同一手势的重复事件。
    // 阈值覆盖系统「双击速度」的慢速设置 (macOS 可调到 ~900ms)。
    if (ctxEl && now - ctxLastAt < 900 &&
        Math.abs(ev.clientX - ctxLastX) <= 10 &&
        Math.abs(ev.clientY - ctxLastY) <= 10) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    ctxLastAt = now;
    ctxLastX = ev.clientX;
    ctxLastY = ev.clientY;
    ev.preventDefault();
    ev.stopPropagation();
    cxShow(ev.clientX, ev.clientY);
  }
  function cxOnPointerDown(ev) {
    if (!ctxEl) { return; }
    if (ctxEl.contains(ev.target)) { return; }
    // 右键/Control+左键的 pointerdown 先于 contextmenu: 不能在 contextmenu
    // 到达前关掉菜单, 否则两指双击的第二下会先关后开。右键统一交给 cxOnContext。
    if (ev.button === 2 || ev.ctrlKey) { return; }
    cxHide();
  }
  function cxOnFocusIn(ev) {
    // Tab 到菜单外时收起, 避免键盘焦点走了菜单还浮在页面上
    if (ctxEl && !ctxEl.contains(ev.target)) { cxHide(); }
  }
  function cxOnKeyDown(ev) {
    if (!ctxEl) { return; }
    if (ev.key === 'Escape') {
      ev.preventDefault();
      cxHide();
      return;
    }
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      var list = Array.prototype.slice.call(ctxEl.querySelectorAll('.fp-ctx-item'));
      if (!list.length) { return; }
      var idx = list.indexOf(document.activeElement);
      var next = ev.key === 'ArrowDown' ? idx + 1 : idx - 1;
      if (next < 0) { next = list.length - 1; }
      if (next >= list.length) { next = 0; }
      list[next].focus();
    }
  }
  el.addEventListener('contextmenu', cxOnContext);
  document.addEventListener('pointerdown', cxOnPointerDown, true);
  document.addEventListener('keydown', cxOnKeyDown, true);
  document.addEventListener('focusin', cxOnFocusIn, true);
  window.addEventListener('scroll', cxHide, true);
  window.addEventListener('resize', cxHide);

  var dispose = function () {
    try { chart.remove(); } catch (e) {}
    cxHide();
    el.removeEventListener('contextmenu', cxOnContext);
    el.removeEventListener('wheel', onPinchWheel, { capture: true });
    el.removeEventListener('gesturestart', onGestureStart);
    el.removeEventListener('gesturechange', onGestureChange);
    el.removeEventListener('gestureend', onGestureEnd);
    document.removeEventListener('pointerdown', cxOnPointerDown, true);
    document.removeEventListener('keydown', cxOnKeyDown, true);
    document.removeEventListener('focusin', cxOnFocusIn, true);
    window.removeEventListener('scroll', cxHide, true);
    window.removeEventListener('resize', cxHide);
  };
  root.__fpChartDispose = dispose;
  return dispose;
}
