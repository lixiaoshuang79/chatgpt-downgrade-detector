// ChatGPT 指纹核验 + 自动改时区 —— content script (v3.0.0 完全独立版，不再依赖 Tampermonkey 油猴脚本)
// v3 新增：由扩展自身 fetch 外部 IP 查询 API（ipapi.co 首选 / ip-api.com 兜底），补齐「出口 IP」详情行
//   （原由油猴脚本提供）；其余 v2 逻辑全部保留。
// 时序设计（关键）：
//  ① document_start 阶段立即按上次核验缓存的期望时区设置 CDP override
//     —— 抢在 OpenAI 前端 JS 采集时区指纹之前生效，页面 JS 首次读时区就是对的，无痕；
//  ② document_idle 后完整核验（/backend-api/me + 浏览器时区），不一致则自动修正；
//  ③ 用户可「恢复系统时区」（暂停自动修正，可随时重新启用）。

(function () {
  'use strict';

  // OpenAI /backend-api/me 的 region 城市 → 期望 IANA 时区映射
  const CITY_TZ = {
    'tokyo': 'Asia/Tokyo', 'osaka': 'Asia/Tokyo',
    'seoul': 'Asia/Seoul', 'incheon': 'Asia/Seoul',
    'taipei': 'Asia/Taipei', 'hong kong': 'Asia/Hong_Kong',
    'singapore': 'Asia/Singapore',
    'toronto': 'America/Toronto', 'montreal': 'America/Toronto',
    'vancouver': 'America/Vancouver',
    'new york': 'America/New_York', 'dallas': 'America/Chicago', 'chicago': 'America/Chicago',
    'los angeles': 'America/Los_Angeles', 'san jose': 'America/Los_Angeles', 'san francisco': 'America/Los_Angeles', 'seattle': 'America/Los_Angeles',
    'paris': 'Europe/Paris', 'frankfurt': 'Europe/Berlin', 'amsterdam': 'Europe/Amsterdam', 'london': 'Europe/London',
    'kyiv': 'Europe/Kyiv', 'istanbul': 'Europe/Istanbul', 'kolkata': 'Asia/Kolkata'
  };
  // 城市查不到时的国家兜底（仅单时区国家/地区；多时区大国不兜底，避免错判）
  const COUNTRY_TZ = {
    'kr': 'Asia/Seoul', 'jp': 'Asia/Tokyo', 'tw': 'Asia/Taipei',
    'hk': 'Asia/Hong_Kong', 'sg': 'Asia/Singapore', 'mo': 'Asia/Macau',
    'fr': 'Europe/Paris', 'de': 'Europe/Berlin', 'gb': 'Europe/London',
    'nl': 'Europe/Amsterdam', 'ua': 'Europe/Kyiv', 'tr': 'Europe/Istanbul',
    'in': 'Asia/Kolkata', 'kr': 'Asia/Seoul'
  };

  const STYLE = `
    #cgtz-check{position:fixed;top:12px;right:12px;z-index:2147483647;max-width:400px;
      font:12px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#fff;color:#111;border-radius:10px;padding:12px 14px;
      box-shadow:0 4px 18px rgba(0,0,0,.28);border:1px solid #e2e2e2}
    #cgtz-check.ok{border-left:4px solid #2e9e5b}
    #cgtz-check.bad{border-left:4px solid #d93025}
    #cgtz-check h4{margin:0 0 8px;font-size:13px;font-weight:600}
    #cgtz-check table{border-collapse:collapse;width:100%}
    #cgtz-check td{padding:2px 4px;vertical-align:top;word-break:break-all}
    #cgtz-check td:first-child{color:#666;white-space:nowrap}
    #cgtz-check .close{position:absolute;top:6px;right:10px;cursor:pointer;color:#999;font-size:15px;line-height:1}
    #cgtz-check .close:hover{color:#333}
    #cgtz-check .hint{margin-top:6px;padding-top:6px;border-top:1px dashed #ddd;color:#b45309}
    #cgtz-check .btnrow{margin-top:8px;display:flex;gap:8px}
    #cgtz-check .btn{border:0;border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}
    #cgtz-check .btn-fix{background:#1a73e8;color:#fff}
    #cgtz-check .btn-fix:hover{background:#1765cc}
    #cgtz-check .btn-fix:disabled{background:#9db8e8;cursor:default}
    #cgtz-check .btn-refresh{background:#f1f3f4;color:#444}
    #cgtz-check .btn-refresh:hover{background:#e3e6e8}
    #cgtz-check .btn-reset{background:#fce8e6;color:#c5221f}
    #cgtz-check .btn-reset:hover{background:#f9d7d4}
    #cgtz-check .fixing{color:#1a73e8;margin-top:6px}
    #cgtz-check .fixed{color:#188038;margin-top:6px;font-weight:600}
    #cgtz-check .err{color:#c5221f;margin-top:6px}`;

  // ============ 阶段①：document_start 立即按缓存期望时区提前修正 ============
  // 页面 JS 尚未执行时把 CDP override 设好；幂等（background 已 attach 且同时区时直接成功）。
  // 多时机重试：SW 冷启动可能较慢，document_start / DOMContentLoaded / load 各发一次，总有一次赶上
  // OpenAI 前端 JS 采集时区指纹之前。
  function tryEarlyFix() {
    try {
      chrome.storage.local.get(['lastTz', 'autoFixPaused'], function (s) {
        if (!s.autoFixPaused && s.lastTz) {
          chrome.runtime.sendMessage({ type: 'FIX_TZ', tz: s.lastTz }).catch(function () {});
        }
      });
    } catch (e) {}
  }
  tryEarlyFix();
  document.addEventListener('DOMContentLoaded', function () { setTimeout(tryEarlyFix, 0); });
  window.addEventListener('load', function () { setTimeout(tryEarlyFix, 0); });

  // ============ 阶段②：核验 + 自动修正 ============

  function injectStyle() {
    const s = document.createElement('style');
    s.textContent = STYLE;
    (document.head || document.documentElement).appendChild(s);
  }

  function fmtOffset(min) {
    const sign = min >= 0 ? '+' : '-';
    min = Math.abs(min);
    return 'UTC' + sign + String(Math.floor(min / 60)).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0');
  }

  function getLocal() {
    return {
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone || '(未知)',
      offset: fmtOffset(-new Date().getTimezoneOffset()),
      lang: navigator.language
    };
  }

  async function getMe() {
    try {
      const r = await fetch('/backend-api/me', { headers: { 'accept': 'application/json' } });
      if (!r.ok) return null;
      const j = await r.json();
      return { country: j.country, region: j.region };
    } catch (e) { return null; }
  }

  // v3.1：出口 IP 查询改走 background（content script 的跨域 fetch 被页面 CSP 拦截，SW 侧不受限）
  async function getIpInfo() {
    try {
      const res = await chrome.runtime.sendMessage({ type: 'GET_IP' });
      if (res && res.ok && res.data) return res.data;
    } catch (e) {}
    return null;
  }

  function expectTzFrom(me) {
    if (me && me.region) {
      const t = CITY_TZ[String(me.region).toLowerCase()];
      if (t) return t;
    }
    if (me && me.country) {
      const t = COUNTRY_TZ[String(me.country).toLowerCase()];
      if (t) return t;
    }
    return null;
  }

  let box = null;
  let fixing = false;
  let autoFixPaused = false; // 用户「恢复系统时区」后暂停自动修正（持久化，刷新/重启后仍保持，可手动重新启用）

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  // 核心：把浏览器时区改为 expectTz（自动/手动共用）
  async function applyFix(expectTz, silent) {
    if (fixing) return false;
    fixing = true;
    const status = box ? box.querySelector('.status') : null;
    const fixBtn = document.getElementById('cgtz-fix');
    if (fixBtn) fixBtn.disabled = true;
    if (status) {
      status.className = 'fixing';
      status.textContent = silent ? '' : '⚡ 正在修正：把浏览器时区改为 ' + expectTz + ' …';
    }
    try {
      const res = await chrome.runtime.sendMessage({ type: 'FIX_TZ', tz: expectTz });
      if (res && res.ok) {
        if (status && !silent) {
          status.className = 'fixed';
          status.textContent = '✅ 已改为 ' + expectTz + '，重新核验中…';
        }
        setTimeout(run, 800); // 重跑核验，应为绿色
        return true;
      }
      if (status && !silent) {
        status.className = 'err';
        status.textContent = '❌ 修正失败：' + ((res && res.err) || '未知错误') + '（可点「重新核验」重试）';
      }
      if (fixBtn) fixBtn.disabled = false;
      return false;
    } catch (e) {
      if (status && !silent) {
        status.className = 'err';
        status.textContent = '❌ 修正失败：' + e.message + '（扩展已禁用？）';
      }
      if (fixBtn) fixBtn.disabled = false;
      return false;
    } finally {
      fixing = false;
    }
  }

  function render(state) {
    // state: { me, local, expectTz, ip }
    const match = state.expectTz && state.expectTz === state.local.tz;
    const rows = [
      ['浏览器时区', esc(state.local.tz) + ' (' + esc(state.local.offset) + ')'],
      ['语言', esc(state.local.lang)]
    ];
    if (state.me && state.me.country) rows.push(['OpenAI 视角', esc(state.me.country) + ' / ' + esc(state.me.region || '?')]);
    else rows.push(['OpenAI 视角', '(me 接口失败：未登录或网络异常)']);

    // v3：出口 IP 详情行（原由油猴脚本提供）
    if (state.ip) {
      rows.push(['出口 IP', esc((state.ip.city ? state.ip.city + ', ' : '') + state.ip.country) + '（' + esc(state.ip.ip) + '）']);
      rows.push(['ASN/运营商', esc(state.ip.org || '(未知)')]);
      rows.push(['IP 时区', esc(state.ip.timezone || '(未知)')]);
    } else {
      rows.push(['出口 IP', '(查询失败)']);
    }
    // 分流异常提示：OpenAI 视角城市（me 接口 region）与外部 IP 查询城市不一致 → Clash 分流不一致，以 OpenAI 视角为准
    if (state.me && state.me.region && state.ip && state.ip.city &&
        String(state.me.region).toLowerCase() !== String(state.ip.city).toLowerCase()) {
      rows.push(['⚠️ 分流注意', 'OpenAI 视角=' + esc(state.me.region) + '，IP 查询=' + esc(state.ip.city) +
        '。Clash 分流不一致（ChatGPT 走 AI 规则出口，IP 查询走默认出口），以 OpenAI 视角为准']);
    }

    let title, cls, buttons = '<button class="btn btn-refresh" id="cgtz-refresh">重新核验</button>';
    if (!state.expectTz) {
      title = '⚠️ ChatGPT 指纹核验 — 无法判定';
      cls = 'bad';
      if (state.me && state.me.country) {
        rows.push(['提示', '出口国家/城市不在映射表（' + esc(state.me.country + '/' + (state.me.region || '?')) +
          '），无法确定期望时区。可先按 IP 时区手动核验']);
      } else {
        rows.push(['提示', '拿不到出口位置信息（me 接口失败），请确认已登录']);
      }
    } else if (match) {
      title = '🟢 时区与出口匹配';
      cls = 'ok';
      rows.push(['判定', '期望时区 ' + esc(state.expectTz) + ' = 浏览器时区']);
      rows.push(['模式', '自动修正已开启（进站即按缓存预改）']);
      buttons = '<button class="btn btn-reset" id="cgtz-reset">恢复系统时区</button>' + buttons;
    } else if (autoFixPaused) {
      title = '🔴 时区与出口不匹配（自动修正已暂停）';
      cls = 'bad';
      rows.push(['判定', '期望时区 ' + esc(state.expectTz) + ' ≠ 浏览器时区 ' + esc(state.local.tz) +
        '（OpenAI 看到你在 ' + esc(state.me ? (state.me.country + '/' + state.me.region) : '?') + '）']);
      buttons = '<button class="btn btn-fix" id="cgtz-fix">🔧 重新启用自动修正</button>' + buttons;
    } else {
      title = '🔴 时区与出口不匹配';
      cls = 'bad';
      rows.push(['判定', '期望时区 ' + esc(state.expectTz) + ' ≠ 浏览器时区 ' + esc(state.local.tz) +
        '（OpenAI 看到你在 ' + esc(state.me ? (state.me.country + '/' + state.me.region) : '?') + '）']);
      buttons = '<button class="btn btn-fix" id="cgtz-fix">⚡ 立即修正</button>' + buttons;
    }

    if (!box) {
      // 接管已有核验条（可能来自油猴脚本）避免双条；若出现多个同 id 条（竞态残留），只保留第一个
      const all = document.querySelectorAll('#cgtz-check');
      if (all.length) {
        box = all[0];
        for (let i = 1; i < all.length; i++) all[i].remove();
        box.querySelectorAll('.btnrow,.status').forEach(function (n) { n.remove(); });
      } else {
        box = document.createElement('div');
        box.id = 'cgtz-check';
        document.body.appendChild(box);
      }
    }
    box.className = cls;
    box.innerHTML =
      '<span class="close" title="关闭">✕</span><h4>' + title + '</h4><table>' +
      rows.map(function (r) { return '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td></tr>'; }).join('') +
      '</table>' + '<div class="btnrow">' + buttons + '</div>' +
      '<div class="status"></div>';

    box.querySelector('.close').onclick = function () { box.remove(); box = null; };

    const status = box.querySelector('.status');
    const fixBtn = document.getElementById('cgtz-fix');
    const refreshBtn = document.getElementById('cgtz-refresh');
    const resetBtn = document.getElementById('cgtz-reset');

    if (fixBtn) {
      fixBtn.onclick = async function () {
        if (autoFixPaused) {
          // 重新启用自动修正
          autoFixPaused = false;
          try { await chrome.storage.local.set({ autoFixPaused: false }); } catch (e) {}
          run();
          return;
        }
        applyFix(state.expectTz, false); // 手动立即修正（自动修正失败时的兜底）
      };
    }
    if (refreshBtn) refreshBtn.onclick = run;
    if (resetBtn) {
      resetBtn.onclick = async function () {
        status.className = 'fixing';
        status.textContent = '正在恢复系统时区…';
        try {
          const res = await chrome.runtime.sendMessage({ type: 'RESET_TZ' });
          if (res && res.ok) {
            autoFixPaused = true;
            try { await chrome.storage.local.set({ autoFixPaused: true }); } catch (e) {}
            status.textContent = '已恢复系统时区，自动修正已暂停（可点「重新启用自动修正」恢复）';
            setTimeout(run, 800);
          } else {
            status.className = 'err';
            status.textContent = '恢复失败：' + ((res && res.err) || '');
          }
        } catch (e) {
          status.className = 'err';
          status.textContent = '恢复失败：' + e.message;
        }
      };
    }
  }

  async function run() {
    try {
      const local = getLocal();
      const [me, ip] = await Promise.all([getMe(), getIpInfo()]); // IP 查询与 me 并行，不额外拖慢核验
      const expectTz = expectTzFrom(me);
      render({ me, local, expectTz, ip });
      // 自动修正：不匹配、未暂停、未在修正中 → 自动改（页面加载后兜底；正常情况阶段①已提前改好）
      if (expectTz && expectTz !== local.tz && !autoFixPaused && !fixing) {
        await applyFix(expectTz, true);
      }
    } catch (e) {
      try { chrome.runtime.sendMessage({ type: 'DBG', msg: 'run-error: ' + String((e && e.message) || e) }); } catch (e2) {}
    }
  }

  // 初始化：读持久化的暂停标志
  try {
    chrome.storage.local.get(['autoFixPaused'], function (s) {
      if (s.autoFixPaused) autoFixPaused = true;
    });
  } catch (e) {}

  try {
    injectStyle();
    if (document.readyState === 'complete') setTimeout(run, 1200);
    else window.addEventListener('load', function () { setTimeout(run, 1200); });
  } catch (e) {
    try { chrome.runtime.sendMessage({ type: 'DBG', msg: 'init-error: ' + String((e && e.message) || e) }); } catch (e2) {}
  }
})();
