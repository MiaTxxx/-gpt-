/* ============================================================================
 * mobile.js — Txxx 公益站移动端补丁外挂脚本
 *
 * 加载方式：在 index.html 的 <head> 里、privacy-toggle.js 之后追加
 *   <script src="/mobile.js?v=..." defer></script>
 *
 * 严格遵守"不重复劫持原则"：
 *   - 不 wrap window.fetch / XMLHttpRequest（已由 privacy-toggle.js 接管）
 *   - 仅消费 DOM 与路由信号
 *
 * 模块：
 *   M0  通用工具
 *   M1  viewport 检测（matchMedia + 200ms 去抖）
 *   M2  SPA 路由监听（pushState/replaceState/popstate 包装）
 *   M3  生图页对话抽屉控制器
 *   M4  管理后台阻断遮罩
 *   M5  Service Worker 注册 + 更新 toast
 *   M6  登出钩子（清运行时缓存）
 *   入口：viewport 触发 → 各模块 onChange
 * ========================================================================= */
(function () {
  'use strict';

  if (window.__MUX__) return; // 防止重复注入
  var MUX = (window.__MUX__ = {});

  var BP = 768;
  MUX.BP = BP;

  // ---------------------------------------------------------------------- M0
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function ce(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'style' && typeof attrs[k] === 'object') {
        for (var sk in attrs[k]) el.style[sk] = attrs[k][sk];
      } else if (k.indexOf('on') === 0 && typeof attrs[k] === 'function') {
        el.addEventListener(k.slice(2), attrs[k]);
      } else if (k === 'aria' && typeof attrs[k] === 'object') {
        for (var ak in attrs[k]) el.setAttribute('aria-' + ak, attrs[k][ak]);
      } else if (k === 'data' && typeof attrs[k] === 'object') {
        for (var dk in attrs[k]) el.setAttribute('data-' + dk, attrs[k][dk]);
      } else {
        el.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      if (!Array.isArray(children)) children = [children];
      children.forEach(function (c) {
        if (c == null) return;
        el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
    }
    return el;
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments, ctx = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  // ---------------------------------------------------------------------- M1
  function createViewport(bp) {
    var mq = window.matchMedia('(max-width: ' + bp + 'px)');
    var listeners = new Set();
    var isMobile = mq.matches;
    var fire = function () {
      listeners.forEach(function (fn) {
        try { fn(isMobile); } catch (_) {}
      });
    };
    var onChange = debounce(function () {
      var next = mq.matches;
      if (next !== isMobile) {
        isMobile = next;
        fire();
      }
    }, 200);
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
    window.addEventListener('resize', onChange, { passive: true });
    window.addEventListener('orientationchange', onChange, { passive: true });

    return {
      get isMobile() { return isMobile; },
      onChange: function (fn) {
        listeners.add(fn);
        try { fn(isMobile); } catch (_) {}
        return function () { listeners.delete(fn); };
      }
    };
  }

  // ---------------------------------------------------------------------- M2
  function createRouter() {
    var listeners = new Set();
    var current = location.pathname;
    var fire = function () {
      var next = location.pathname;
      if (next === current) {
        // 即使 path 没变也通知一次（因为 query/hash 可能变了）
      }
      current = next;
      listeners.forEach(function (fn) {
        try { fn(current); } catch (_) {}
      });
    };
    function wrap(key) {
      var orig = history[key];
      if (typeof orig !== 'function') return;
      history[key] = function () {
        var r = orig.apply(this, arguments);
        setTimeout(fire, 0);
        return r;
      };
    }
    wrap('pushState');
    wrap('replaceState');
    window.addEventListener('popstate', function () { setTimeout(fire, 0); });

    return {
      get path() { return location.pathname; },
      onPathChange: function (fn) {
        listeners.add(fn);
        try { fn(location.pathname); } catch (_) {}
        return function () { listeners.delete(fn); };
      }
    };
  }

  // ---------------------------------------------------------------------- M3
  // 抽屉手势识别（纯函数，便于测试）
  function recognize(seq, isOpen, viewportWidth) {
    if (!seq || !seq.start || !seq.end) return 'noop';
    var dx = seq.end.x - seq.start.x;
    var dy = Math.abs((seq.end.y || 0) - (seq.start.y || 0));
    var ratio = Math.abs(dx) / Math.max(dy, 1);
    if (!isOpen && seq.start.x < 24 && dx > 40 && ratio > 1.5) return 'open';
    if (isOpen && -dx > 40 && ratio > 1.5) return 'close';
    return 'noop';
  }
  MUX.recognize = recognize;

  function createGenerationDrawer(viewport, router) {
    var STATE = { closed: 'closed', open: 'open' };
    var state = STATE.closed;
    var fab = null;
    var overlay = null;
    var drawer = null;
    var sidebar = null;
    var sidebarOriginal = null; // { parent, nextSibling }
    var touchStart = null;
    var touchMoves = [];
    var pushedHistory = false;

    function isGenerationRoute() {
      return router.path && router.path.indexOf('/workspace/generate') === 0;
    }

    function lockBody() {
      document.body.classList.add('mux-drawer-open');
    }
    function unlockBody() {
      document.body.classList.remove('mux-drawer-open');
    }

    function ensureFab() {
      if (fab) return;
      fab = ce('button', {
        type: 'button',
        class: 'mux-fab',
        'aria-label': '打开历史对话'
      }, [
        // 简单 SVG 汉堡 / 对话气泡
        (function () {
          var svgNS = 'http://www.w3.org/2000/svg';
          var svg = document.createElementNS(svgNS, 'svg');
          svg.setAttribute('viewBox', '0 0 24 24');
          svg.setAttribute('aria-hidden', 'true');
          var path = document.createElementNS(svgNS, 'path');
          path.setAttribute('d', 'M4 7h16M4 12h16M4 17h10');
          path.setAttribute('stroke', 'currentColor');
          path.setAttribute('stroke-width', '2');
          path.setAttribute('stroke-linecap', 'round');
          path.setAttribute('fill', 'none');
          svg.appendChild(path);
          return svg;
        })()
      ]);
      fab.addEventListener('click', open);
      document.body.appendChild(fab);
    }

    function ensureOverlayAndDrawer() {
      if (overlay && drawer) return;

      overlay = ce('div', { class: 'mux-overlay', 'aria-hidden': 'true' });
      overlay.addEventListener('click', close);

      drawer = ce('aside', {
        class: 'mux-drawer',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-label': '历史对话'
      }, [
        ce('div', { class: 'mux-drawer__head' }, [
          ce('h3', null, '历史对话'),
          ce('button', {
            type: 'button',
            class: 'mux-drawer__close',
            'aria-label': '关闭'
          }, [
            (function () {
              var svgNS = 'http://www.w3.org/2000/svg';
              var svg = document.createElementNS(svgNS, 'svg');
              svg.setAttribute('viewBox', '0 0 24 24');
              svg.setAttribute('width', '20');
              svg.setAttribute('height', '20');
              var p = document.createElementNS(svgNS, 'path');
              p.setAttribute('d', 'M6 6 L18 18 M18 6 L6 18');
              p.setAttribute('stroke', 'currentColor');
              p.setAttribute('stroke-width', '2');
              p.setAttribute('stroke-linecap', 'round');
              p.setAttribute('fill', 'none');
              svg.appendChild(p);
              return svg;
            })()
          ])
        ]),
        ce('div', { class: 'mux-drawer__body' })
      ]);
      drawer.querySelector('.mux-drawer__close').addEventListener('click', close);

      document.body.appendChild(overlay);
      document.body.appendChild(drawer);
    }

    function moveSidebarIn() {
      sidebar = $('.conv-sidebar');
      if (!sidebar || !drawer) return;
      // 已经在抽屉里？
      if (sidebar.parentNode && sidebar.parentNode.classList && sidebar.parentNode.classList.contains('mux-drawer__body')) return;
      sidebarOriginal = { parent: sidebar.parentNode, nextSibling: sidebar.nextSibling };
      var body = drawer.querySelector('.mux-drawer__body');
      body.appendChild(sidebar);
    }

    function restoreSidebar() {
      if (!sidebar || !sidebarOriginal) return;
      try {
        if (sidebarOriginal.parent && sidebarOriginal.parent.isConnected) {
          if (sidebarOriginal.nextSibling && sidebarOriginal.nextSibling.parentNode === sidebarOriginal.parent) {
            sidebarOriginal.parent.insertBefore(sidebar, sidebarOriginal.nextSibling);
          } else {
            sidebarOriginal.parent.appendChild(sidebar);
          }
        }
      } catch (_) {}
      sidebar = null;
      sidebarOriginal = null;
    }

    function open() {
      if (state === STATE.open) return;
      ensureOverlayAndDrawer();
      moveSidebarIn();
      state = STATE.open;
      // 下一帧加 is-open 触发 transition
      requestAnimationFrame(function () {
        if (overlay) overlay.classList.add('is-open');
        if (drawer) drawer.classList.add('is-open');
      });
      lockBody();
      try {
        history.pushState({ muxDrawer: 1 }, '', location.href);
        pushedHistory = true;
      } catch (_) {}
    }

    function closeImmediate() {
      state = STATE.closed;
      if (overlay) overlay.classList.remove('is-open');
      if (drawer) drawer.classList.remove('is-open');
      unlockBody();
      // sidebar 保持在抽屉里以便下次直接展开；只有退出 generation 路由或 desktop 时才还原
    }

    function close() {
      if (state === STATE.closed) return;
      closeImmediate();
      if (pushedHistory) {
        pushedHistory = false;
        try { history.back(); } catch (_) {}
      }
    }

    function onPopState() {
      if (state === STATE.open) {
        // 用户/系统的返回键被按下
        pushedHistory = false;
        closeImmediate();
      }
    }

    // 触摸手势
    function onTouchStart(e) {
      if (!viewport.isMobile || !isGenerationRoute()) return;
      var t = e.touches[0];
      touchStart = { x: t.clientX, y: t.clientY };
      touchMoves = [];
    }
    function onTouchMove(e) {
      if (!touchStart) return;
      var t = e.touches[0];
      touchMoves.push({ x: t.clientX, y: t.clientY });
    }
    function onTouchEnd() {
      if (!touchStart) return;
      var end = touchMoves.length ? touchMoves[touchMoves.length - 1] : touchStart;
      var seq = { start: touchStart, end: end, moves: touchMoves };
      var action = recognize(seq, state === STATE.open, window.innerWidth);
      touchStart = null;
      touchMoves = [];
      if (action === 'open') open();
      else if (action === 'close') close();
    }

    function attach() {
      window.addEventListener('popstate', onPopState);
      window.addEventListener('touchstart', onTouchStart, { passive: true });
      window.addEventListener('touchmove', onTouchMove, { passive: true });
      window.addEventListener('touchend', onTouchEnd, { passive: true });
    }
    function detach() {
      window.removeEventListener('popstate', onPopState);
      window.removeEventListener('touchstart', onTouchStart);
      window.removeEventListener('touchmove', onTouchMove);
      window.removeEventListener('touchend', onTouchEnd);
    }

    function update() {
      var should = viewport.isMobile && isGenerationRoute();
      if (should) {
        ensureFab();
      } else {
        // 退出：还原
        if (state === STATE.open) closeImmediate();
        restoreSidebar();
        if (fab && fab.parentNode) fab.parentNode.removeChild(fab); fab = null;
        if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay); overlay = null;
        if (drawer && drawer.parentNode) drawer.parentNode.removeChild(drawer); drawer = null;
      }
    }

    attach();
    viewport.onChange(update);
    router.onPathChange(update);

    // SPA 路由切换可能是在 conv-sidebar 渲染之前发生的：抽屉若已展开就尝试再次拉取 sidebar
    var observer = new MutationObserver(function () {
      if (state === STATE.open && drawer && !drawer.querySelector('.conv-sidebar')) {
        moveSidebarIn();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    return {
      open: open, close: close,
      get state() { return state; },
      destroy: function () {
        detach();
        observer.disconnect();
        update();
      }
    };
  }

  // ---------------------------------------------------------------------- M4
  function createAdminBlock(viewport, router) {
    var el = null;
    function shouldShow() {
      var p = router.path || '';
      return viewport.isMobile && p.indexOf('/admin') === 0
        && sessionStorage.getItem('mux:bypass-admin-block') !== '1';
    }
    function render() {
      var panel = ce('div', {
        class: 'mux-block',
        role: 'dialog',
        'aria-modal': 'true'
      }, [
        ce('div', { class: 'mux-block__panel' }, [
          ce('h2', null, '管理后台请用电脑访问'),
          ce('p', null, '本页面在窄屏下功能受限（注册机、表格、SSE 事件流等），强烈建议使用桌面浏览器访问。'),
          ce('div', { class: 'mux-block__btns' }, [
            ce('a', { class: 'mux-block__btn', href: '/' }, '返回首页'),
            ce('button', {
              type: 'button',
              class: 'mux-block__btn mux-block__btn--ghost',
              onclick: function () {
                sessionStorage.setItem('mux:bypass-admin-block', '1');
                update();
              }
            }, '我知道了，强行查看（仅当前会话）')
          ])
        ])
      ]);
      // 阻止合成 click 穿透
      ['click', 'touchstart', 'pointerdown'].forEach(function (evt) {
        panel.addEventListener(evt, function (e) { e.stopPropagation(); });
      });
      return panel;
    }
    function update() {
      var need = shouldShow();
      if (need && !el) {
        el = render();
        document.body.appendChild(el);
      }
      if (!need && el) {
        el.parentNode && el.parentNode.removeChild(el);
        el = null;
      }
    }
    viewport.onChange(update);
    router.onPathChange(update);
    return { update: update, destroy: function () { if (el) el.remove(); el = null; } };
  }

  // ---------------------------------------------------------------------- M5
  function createSwRegistrar() {
    if (!('serviceWorker' in navigator)) return { update: function () {} };

    var hasManifest = !!document.querySelector('link[rel="manifest"]');
    var protoOk = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    if (!hasManifest || !protoOk) return { update: function () {} };

    var registered = false;
    var attempts = 0;

    function showUpdateToast(reg) {
      if (document.querySelector('.mux-toast[data-kind="update"]')) return;
      var toast = ce('div', { class: 'mux-toast', data: { kind: 'update' } }, [
        ce('span', null, '有新版本可用'),
        ce('button', {
          type: 'button',
          class: 'mux-toast__btn',
          onclick: function () {
            try { reg.waiting && reg.waiting.postMessage({ type: 'SKIP_WAITING' }); } catch (_) {}
          }
        }, '点击刷新'),
        ce('button', {
          type: 'button',
          class: 'mux-toast__close',
          'aria-label': '稍后',
          onclick: function () { hideToast(toast); }
        }, '×')
      ]);
      document.body.appendChild(toast);
      requestAnimationFrame(function () { toast.classList.add('is-show'); });
    }
    function hideToast(toast) {
      toast.classList.remove('is-show');
      setTimeout(function () { toast.remove(); }, 300);
    }

    function wireUpdateFlow(reg) {
      navigator.serviceWorker.addEventListener('controllerchange', function () {
        // 新 SW 接管 → 整页刷新拿最新资源
        if (window.__MUX_RELOADING__) return;
        window.__MUX_RELOADING__ = true;
        location.reload();
      });
      if (reg.waiting) showUpdateToast(reg);
      reg.addEventListener('updatefound', function () {
        var sw = reg.installing;
        if (!sw) return;
        sw.addEventListener('statechange', function () {
          if (sw.state === 'installed' && navigator.serviceWorker.controller) {
            showUpdateToast(reg);
          }
        });
      });
      // 兜底周期性 update
      setInterval(function () { reg.update().catch(function () {}); }, 30 * 60 * 1000);
    }

    function tryRegister() {
      navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' })
        .then(function (reg) {
          registered = true;
          wireUpdateFlow(reg);
        })
        .catch(function () {
          if (attempts++ < 1) setTimeout(tryRegister, 3000);
        });
    }

    if (document.readyState === 'complete') tryRegister();
    else window.addEventListener('load', tryRegister);

    return {
      update: function () {
        if (!registered) return;
        navigator.serviceWorker.getRegistration('/').then(function (reg) {
          reg && reg.update().catch(function () {});
        });
      }
    };
  }

  // ---------------------------------------------------------------------- M6
  function createLogoutHook() {
    if (!window.localStorage) return;
    var origRemoveItem = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function (key) {
      if (this === localStorage && key === 'token') {
        try {
          if (window.caches && caches.keys) {
            caches.keys().then(function (keys) {
              keys.filter(function (k) { return k.indexOf('runtime-') === 0; })
                .forEach(function (k) { caches.delete(k); });
            });
          }
        } catch (_) {}
      }
      return origRemoveItem.apply(this, arguments);
    };
  }

  // ---------------------------------------------------------------------- M7
  // 给受保护接口的 window.open 自动补 token query：
  // 前端 ImageManagerView 用 window.open(`/api/images/${id}/download`, '_blank')
  // 打开新标签下载，但浏览器不会带 axios 的 Authorization 头 → 后端 401
  // 后端 dependencies.get_current_user_query_or_header 已支持 ?token=xxx
  // 这里在前端这一侧把 token 透明拼上。
  function createWindowOpenAuthHook() {
    if (typeof window.open !== 'function') return;
    var origOpen = window.open.bind(window);
    var PROTECTED_RE = /^\/api\/(images|uploads)\/[^?#]*\/download(\?|$|#)/;
    window.open = function (url, target, features) {
      try {
        if (typeof url === 'string' && PROTECTED_RE.test(url)) {
          var token = localStorage.getItem('token');
          if (token && url.indexOf('token=') < 0) {
            var sep = url.indexOf('?') >= 0 ? '&' : '?';
            url = url + sep + 'token=' + encodeURIComponent(token);
          }
        }
      } catch (_) {}
      return origOpen(url, target, features);
    };
  }

  // ---------------------------------------------------------------------- 入口
  function boot() {
    var viewport = createViewport(BP);
    var router = createRouter();
    MUX.viewport = viewport;
    MUX.router = router;
    MUX.drawer = createGenerationDrawer(viewport, router);
    MUX.adminBlock = createAdminBlock(viewport, router);
    MUX.sw = createSwRegistrar();
    createLogoutHook();
    createWindowOpenAuthHook();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
