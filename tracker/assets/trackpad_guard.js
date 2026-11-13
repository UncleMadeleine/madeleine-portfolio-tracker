
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
