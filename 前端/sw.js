/* ============================================================================
 * sw.js — Txxx 公益站 Service Worker
 *
 * 作用域：/
 * 注册位置：mobile.js 在 window.load 后调 register('/sw.js', { scope: '/' })
 *
 * 缓存策略：
 *   - App_Shell：install 阶段预缓存 / 与 PWA 资源
 *   - 导航请求：network-first(timeout=3s) → APP_SHELL → /offline.html
 *   - /assets/*：cache-first + 后台更新
 *   - /images/*、/uploads/*：network-first，仅会话内 RUNTIME_IMG，不持久化
 *   - Sensitive_API、Authorization、SSE、非 GET、跨源：bypass
 *
 * kill switch：
 *   activate 阶段拉 /index.html，若不含 "/mobile.js" 即 unregister + 清空所有 caches。
 * ========================================================================= */

const VERSION = '2026-05-17-1';
const APP_SHELL = `app-shell-v${VERSION}`;
const ASSETS = `assets-v${VERSION}`;
const RUNTIME_IMG = `runtime-images-v${VERSION}`;

const SHELL_URLS = [
  '/',
  '/offline.html',
  '/favicon.svg',
  '/manifest.json',
  '/mobile.css',
  '/mobile.js',
  '/privacy-toggle.js',
  '/favicon-192.png',
  '/favicon-192-maskable.png',
  '/favicon-512.png',
  '/favicon-512-maskable.png',
  '/apple-touch-icon-180.png'
];

const SENSITIVE_RE = /^\/(api\/(auth|users\/me|conversations|generate|feedback|inbox|admin|images|gallery)|oauth\/callback)/i;

/**
 * 纯函数：根据请求决定使用哪种策略。
 * 返回：{ strategy, cacheName?, fallbackUrl? }
 *   strategy ∈ 'bypass' | 'navigation' | 'asset-cache-first'
 *            | 'image-network-first' | 'static-network-first'
 */
function classifyRequest(method, urlStr, headers, mode) {
  const url = new URL(urlStr, self.location.origin);

  // 跨源
  if (url.origin !== self.location.origin) return { strategy: 'bypass' };
  // 非 GET
  if (method && method.toUpperCase() !== 'GET') return { strategy: 'bypass' };
  // Authorization
  if (headers && (headers.get ? headers.get('Authorization') : headers['Authorization'])) {
    return { strategy: 'bypass' };
  }
  // SSE
  const accept = headers && (headers.get ? headers.get('Accept') : headers['Accept']) || '';
  if (accept && accept.indexOf('text/event-stream') >= 0) return { strategy: 'bypass' };
  // Sensitive API
  if (SENSITIVE_RE.test(url.pathname)) return { strategy: 'bypass' };
  // SW 自身永不缓存
  if (url.pathname === '/sw.js') return { strategy: 'bypass' };

  // 导航
  if (mode === 'navigate') {
    return { strategy: 'navigation', cacheName: APP_SHELL, fallbackUrl: '/offline.html' };
  }
  // 静态构建产物
  if (url.pathname.indexOf('/assets/') === 0) {
    return { strategy: 'asset-cache-first', cacheName: ASSETS };
  }
  // 用户图片：私密敏感，会话内运行时缓存
  if (url.pathname.indexOf('/images/') === 0 || url.pathname.indexOf('/uploads/') === 0) {
    return { strategy: 'image-network-first', cacheName: RUNTIME_IMG };
  }
  // 其他根静态（含 /, /manifest.json, /offline.html, /favicon-*.png, /privacy-toggle.js, /mobile.* 等）
  return { strategy: 'static-network-first', cacheName: APP_SHELL };
}

self.__MUX_SW__ = { classifyRequest, SENSITIVE_RE, VERSION, APP_SHELL, ASSETS, RUNTIME_IMG };

// ---------- install ----------
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(APP_SHELL).then(function (cache) {
      // 用 add 单独写入，任一失败不影响其它（更稳）
      return Promise.all(SHELL_URLS.map(function (u) {
        return cache.add(new Request(u, { cache: 'reload' })).catch(function () {});
      }));
    })
    // 不调 skipWaiting，由前端 toast 用户主动触发
  );
});

// ---------- activate ----------
self.addEventListener('activate', function (event) {
  event.waitUntil((async function () {
    const expected = new Set([APP_SHELL, ASSETS, RUNTIME_IMG]);
    const keys = await caches.keys();

    // 删除非当前版本缓存键
    await Promise.all(keys.map(function (k) {
      return expected.has(k) ? null : caches.delete(k);
    }));

    // 二次扫描：清理所有 cache 中可能残留的 Sensitive 条目
    for (const name of await caches.keys()) {
      const c = await caches.open(name);
      const reqs = await c.keys();
      await Promise.all(reqs.map(function (req) {
        try {
          const u = new URL(req.url);
          if (u.origin === self.location.origin && SENSITIVE_RE.test(u.pathname)) {
            return c.delete(req);
          }
        } catch (_) {}
        return null;
      }));
    }

    // kill switch：前端已经撤回 mobile.js 引用 → 自我注销
    try {
      const r = await fetch('/index.html', { cache: 'no-store' });
      if (r && r.ok) {
        const html = await r.text();
        if (html.indexOf('/mobile.js') < 0) {
          for (const k of await caches.keys()) await caches.delete(k);
          if (self.registration && self.registration.unregister) {
            await self.registration.unregister();
          }
          return;
        }
      }
    } catch (_) {
      // 拉不到 index.html 时保守：不卸载
    }

    await self.clients.claim();
  })());
});

// ---------- message：用于触发 SKIP_WAITING ----------
self.addEventListener('message', function (event) {
  if (event && event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ---------- fetch ----------
self.addEventListener('fetch', function (event) {
  const req = event.request;
  const decision = classifyRequest(req.method, req.url, req.headers, req.mode);

  if (decision.strategy === 'bypass') return; // 不调 respondWith → 浏览器走默认网络

  event.respondWith(handle(req, decision));
});

function timeoutFetch(req, ms) {
  return new Promise(function (resolve, reject) {
    var t = setTimeout(function () { reject(new Error('timeout')); }, ms);
    fetch(req).then(function (r) { clearTimeout(t); resolve(r); }, function (e) {
      clearTimeout(t); reject(e);
    });
  });
}

async function handle(req, decision) {
  const cache = await caches.open(decision.cacheName);

  if (decision.strategy === 'navigation') {
    // 网络优先 3s，超时回退缓存，再回退 /offline.html
    try {
      const net = await timeoutFetch(req, 3000);
      if (net && net.ok) {
        try { await cache.put('/', net.clone()); } catch (_) {}
      }
      return net;
    } catch (_) {
      const cached = await cache.match('/') || await cache.match(req);
      if (cached) return cached;
      return (await cache.match('/offline.html')) || new Response(
        '<!doctype html><meta charset="utf-8"><title>Offline</title><p>网络不可用，请刷新重试。',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      );
    }
  }

  if (decision.strategy === 'asset-cache-first') {
    const cached = await cache.match(req);
    const fetchAndUpdate = fetch(req).then(function (r) {
      if (r && r.ok) try { cache.put(req, r.clone()); } catch (_) {}
      return r;
    }).catch(function () { return null; });
    if (cached) {
      // 后台更新
      fetchAndUpdate;
      return cached;
    }
    const net = await fetchAndUpdate;
    return net || new Response('', { status: 504 });
  }

  if (decision.strategy === 'image-network-first') {
    try {
      const net = await timeoutFetch(req, 3000);
      if (net && net.ok) {
        // 仅在响应不含 Cache-Control: private 时写入运行时缓存
        var cc = (net.headers.get('Cache-Control') || '').toLowerCase();
        if (cc.indexOf('private') < 0) {
          try { cache.put(req, net.clone()); } catch (_) {}
        }
      }
      return net;
    } catch (_) {
      const cached = await cache.match(req);
      if (cached) return cached;
      return new Response('', { status: 504 });
    }
  }

  // static-network-first
  try {
    const net = await timeoutFetch(req, 3000);
    if (net && net.ok) try { cache.put(req, net.clone()); } catch (_) {}
    return net;
  } catch (_) {
    const cached = await cache.match(req);
    if (cached) return cached;
    return new Response('', { status: 504 });
  }
}
