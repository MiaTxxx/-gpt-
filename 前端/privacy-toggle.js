/* 注入两件功能：
   1. 隐私模式开关（嵌入到生图页 .input-toolbar）
   2. 把所有发往 POST /api/generate 的请求改造成异步：
        - 实际请求改打 /api/generate/async 拿 task_id
        - 后台轮询 /api/generate/task/{id} 直到完成
        - 把最终结果"伪装成同步接口的响应"返还给前端代码
      绕过 Cloudflare 100s 上限。

   关键点：前端 axios 默认走 XMLHttpRequest（不走 fetch），所以这里同时拦截 XHR + fetch。 */
(function () {
  const PRIV_KEY = 'site_private_mode';
  const isPriv = () => localStorage.getItem(PRIV_KEY) === '1';

  const ASYNC_POLL_MS = 2000;
  const ASYNC_MAX_WAIT_MS = 5 * 60 * 1000;

  const TARGET_RE = /\/api\/generate(\?|$)/;

  // 把响应体里的相对图片路径 /images/... 改成绝对 URL，避免前端正则
  // /(https?:\/\/...)/  匹配不到
  function absolutize(text) {
    if (typeof text !== 'string' || text.indexOf('/images/') < 0) return text;
    const origin = location.origin;
    // 把 "/images/xxx.png" 形式（含转义）替换成 "https://host/images/xxx.png"
    // 只动以 / 开头并紧跟 images/ 的路径，不动已经带 http(s) 的
    return text
      .replace(/(["'(\s,>])(\/images\/[A-Za-z0-9._\-\/]+)/g, (_, lead, p) => lead + origin + p)
      .replace(/(["'(\s,>])(\/uploads\/[A-Za-z0-9._\-\/]+)/g, (_, lead, p) => lead + origin + p);
  }

  // ====================================================================
  // CSS
  // ====================================================================
  function injectCss() {
    if (document.getElementById('pt-style')) return;
    const css = `
      .privacy-chip{
        display:inline-flex;align-items:center;gap:6px;
        padding:6px 12px;border-radius:999px;
        background:#eef2f6;color:#5a6372;
        font-size:12px;line-height:1;cursor:pointer;
        border:1px solid transparent;
        transition:all .15s ease;white-space:nowrap;user-select:none;
      }
      .privacy-chip:hover{background:#e4e9ee;color:#303133}
      .privacy-chip.is-on{
        background:rgba(64,158,255,.12);color:#409eff;border-color:rgba(64,158,255,.3);
      }
      .privacy-chip .pc-dot{width:6px;height:6px;border-radius:50%;background:#bcc4cc}
      .privacy-chip.is-on .pc-dot{background:#409eff;box-shadow:0 0 0 2px rgba(64,158,255,.18)}
      .privacy-chip .pc-icon{font-size:12px;display:inline-flex}
      .dark .privacy-chip,.dark-mode .privacy-chip,html[data-theme="dark"] .privacy-chip{
        background:#2a2f3a;color:#aab1bd;
      }
      .dark .privacy-chip.is-on,.dark-mode .privacy-chip.is-on,html[data-theme="dark"] .privacy-chip.is-on{
        background:rgba(64,158,255,.18);color:#79bbff;
      }
    `;
    const s = document.createElement('style');
    s.id = 'pt-style';
    s.textContent = css;
    document.head.appendChild(s);
  }

  function getToken() { return 'Bearer ' + (localStorage.getItem('token') || ''); }
  function safeParse(t) { try { return JSON.parse(t); } catch (_) { return null; } }

  async function asyncGenerate(bodyObj, headersHook) {
    if (isPriv() && bodyObj.is_private === undefined) bodyObj.is_private = true;
    const baseHeaders = { 'Content-Type': 'application/json', Authorization: getToken() };
    if (headersHook) headersHook(baseHeaders);
    const r = await origFetch('/api/generate/async', {
      method: 'POST',
      body: JSON.stringify(bodyObj),
      headers: baseHeaders,
      credentials: 'same-origin'
    });
    if (!r.ok) {
      const text = await r.text();
      const err = safeParse(text) || { detail: text || '提交失败' };
      throw Object.assign(new Error(err.detail || 'submit failed'), { _status: r.status, _body: err });
    }
    const submitted = await r.json();
    const taskId = submitted.task_id;
    const started = Date.now();
    while (Date.now() - started < ASYNC_MAX_WAIT_MS) {
      const tr = await origFetch('/api/generate/task/' + encodeURIComponent(taskId), {
        headers: { Authorization: getToken() }
      });
      if (!tr.ok) throw Object.assign(new Error('task poll failed'), { _status: tr.status });
      const j = await tr.json();
      if (j.status === 'done' && j.image) return j.image;
      if (j.status === 'failed') throw Object.assign(new Error(j.error || '生成失败'), { _status: 502 });
      await new Promise(res => setTimeout(res, ASYNC_POLL_MS));
    }
    throw Object.assign(new Error('生成超时'), { _status: 504 });
  }

  // ====================================================================
  // fetch 拦截
  // ====================================================================
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input?.url || '');
      const method = (init?.method || (typeof input === 'object' && input?.method) || 'GET').toUpperCase();
      if (method === 'POST' && TARGET_RE.test(url)) {
        let obj = null;
        if (typeof init?.body === 'string') obj = safeParse(init.body);
        if (obj) {
          return (async () => {
            try {
              const result = await asyncGenerate(obj);
              return new Response(absolutize(JSON.stringify(result)), { status: 200, headers: { 'Content-Type': 'application/json' } });
            } catch (e) {
              const status = e._status || 502;
              const body = e._body || { detail: String(e.message || e) };
              return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
            }
          })();
        }
      }
      // 其他 /api/ 响应：把图片相对路径绝对化，让前端正则能识别
      if (/\/api\//.test(url)) {
        return origFetch(input, init).then(async (r) => {
          if (!r.ok) return r;
          const ct = r.headers.get('content-type') || '';
          if (!/json/.test(ct)) return r;
          const text = await r.clone().text();
          const fixed = absolutize(text);
          if (fixed === text) return r;
          return new Response(fixed, { status: r.status, statusText: r.statusText, headers: r.headers });
        });
      }
    } catch (_) {}
    return origFetch(input, init);
  };

  // ====================================================================
  // XMLHttpRequest 拦截（axios 默认走这条）
  // ====================================================================
  const XHR = window.XMLHttpRequest;
  const origOpen = XHR.prototype.open;
  const origSend = XHR.prototype.send;
  const origSetHeader = XHR.prototype.setRequestHeader;

  XHR.prototype.open = function (method, url, ...rest) {
    this.__pt_method = (method || '').toUpperCase();
    this.__pt_url = url || '';
    this.__pt_headers = {};
    return origOpen.call(this, method, url, ...rest);
  };

  XHR.prototype.setRequestHeader = function (k, v) {
    if (this.__pt_headers) this.__pt_headers[String(k).toLowerCase()] = v;
    return origSetHeader.call(this, k, v);
  };

  XHR.prototype.send = function (body) {
    try {
      if (this.__pt_method === 'POST' && TARGET_RE.test(this.__pt_url || '')) {
        const obj = typeof body === 'string' ? safeParse(body) : null;
        if (obj) {
          // 接管：不调 origSend，自己异步走 fetch + 轮询，再 dispatch 假事件
          (async () => {
            const fakeStatus = (status, payload) => {
              try {
                Object.defineProperty(this, 'readyState', { configurable: true, get: () => 4 });
                Object.defineProperty(this, 'status', { configurable: true, get: () => status });
                const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
                Object.defineProperty(this, 'responseText', { configurable: true, get: () => text });
                Object.defineProperty(this, 'response', { configurable: true, get: () => text });
                Object.defineProperty(this, 'getAllResponseHeaders', { configurable: true, value: () => 'content-type: application/json\r\n' });
                Object.defineProperty(this, 'getResponseHeader', { configurable: true, value: (n) => /content-type/i.test(n) ? 'application/json' : null });
                if (typeof this.onreadystatechange === 'function') this.onreadystatechange();
                this.dispatchEvent(new Event('readystatechange'));
                this.dispatchEvent(new Event('load'));
                this.dispatchEvent(new Event('loadend'));
              } catch (_) {}
            };
            try {
              const result = await asyncGenerate(obj, (h) => {
                // 把原 XHR 的 Authorization 复用上（避免重复读 localStorage）
                const auth = this.__pt_headers && (this.__pt_headers.authorization);
                if (auth) h.Authorization = auth;
              });
              fakeStatus(200, absolutize(JSON.stringify(result)));
            } catch (e) {
              const status = e._status || 502;
              const body = e._body || { detail: String(e.message || e) };
              fakeStatus(status, body);
            }
          })();
          return; // 不调用真正的 send
        }
      }

      // 对所有其他 /api/ GET/POST 响应，劫持 responseText 把相对图片路径绝对化
      if (this.__pt_url && /\/api\//.test(this.__pt_url)) {
        const xhr = this;
        const origOnReady = xhr.onreadystatechange;
        const wrapped = function (ev) {
          try {
            if (xhr.readyState === 4 && xhr.status >= 200 && xhr.status < 300) {
              const ct = (xhr.getResponseHeader('content-type') || '').toLowerCase();
              if (ct.indexOf('json') >= 0) {
                const orig = xhr.responseText;
                const fixed = absolutize(orig);
                if (fixed !== orig) {
                  Object.defineProperty(xhr, 'responseText', { configurable: true, get: () => fixed });
                  Object.defineProperty(xhr, 'response', { configurable: true, get: () => fixed });
                }
              }
            }
          } catch (_) {}
          if (origOnReady) return origOnReady.call(xhr, ev);
        };
        xhr.onreadystatechange = wrapped;
      }
    } catch (_) {}
    return origSend.call(this, body);
  };

  // ====================================================================
  // UI: 隐私按钮嵌入 .input-toolbar
  // ====================================================================
  function buildChip() {
    const chip = document.createElement('div');
    chip.className = 'privacy-chip pt-chip';
    chip.innerHTML = '<span class="pc-icon">🔒</span><span class="pc-text">隐私模式</span><span class="pc-dot"></span>';
    chip.addEventListener('click', () => {
      localStorage.setItem(PRIV_KEY, isPriv() ? '0' : '1');
      paint(chip);
    });
    paint(chip);
    return chip;
  }

  function paint(chip) {
    const on = isPriv();
    chip.classList.toggle('is-on', on);
    chip.querySelector('.pc-text').textContent = '隐私模式' + (on ? ' · 开' : '');
    chip.title = on
      ? '隐私模式已开：本次生成的图片不会进入公开提示词借鉴。点击关闭。'
      : '点击开启隐私模式。开启后生成的图片不会进入公开提示词借鉴。';
  }

  function tryMount() {
    injectCss();
    const toolbar = document.querySelector('.input-toolbar');
    if (toolbar && !toolbar.querySelector('.pt-chip')) {
      toolbar.insertBefore(buildChip(), toolbar.firstChild);
    }
    document.querySelectorAll('.pt-chip').forEach(paint);
  }

  const _push = history.pushState;
  history.pushState = function () { const r = _push.apply(this, arguments); setTimeout(tryMount, 0); return r; };
  window.addEventListener('popstate', () => setTimeout(tryMount, 0));

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', tryMount);
  else tryMount();
  setInterval(tryMount, 1500);
})();
