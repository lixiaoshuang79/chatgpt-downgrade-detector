// ChatGPT 指纹核验 + 自动改时区 —— background (MV3 service worker)
// 核心：chrome.debugger attach 到 chatgpt tab → Emulation.setTimezoneOverride 改时区
// 注意：改时区后必须保持 attach，否则时区设置会随 detach 丢失（浏览器顶部会出现调试提示条，属正常）

const CDP_VERSION = '1.3';

// 记录已附加的 tab 与时区（session 级，浏览器重启后清空，需重新附加）
let attached = { tabId: null, tz: null };

function attach(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, CDP_VERSION, () => {
      const err = chrome.runtime.lastError;
      if (err) {
        if (/already attached|Another debugger/i.test(err.message || '')) resolve(); // 已附加，复用
        else reject(new Error(err.message));
      } else resolve();
    });
  });
}

function sendCmd(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (res) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(res);
    });
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === 'DBG') {
    console.log('[cgtz-dbg]', msg.msg);
    sendResponse({ ok: true });
    return;
  }
  if (msg && msg.type === 'GET_IP') {
    // v3.1：出口 IP 查询挪到 background（content script 的 fetch 受页面 CSP 限制，SW 不受）
    // 注意：ipapi.co 对程序化请求弹 Cloudflare 验证；ip-api.com 免费版仅支持 http://（https 返回 fail）
    (async () => {
      try {
        const r1 = await fetch('http://ip-api.com/json/?fields=status,query,country,city,org,timezone', { signal: AbortSignal.timeout(10000) });
        if (r1.ok) {
          const j = await r1.json();
          if (j && j.status === 'success') {
            sendResponse({ ok: true, data: { ip: j.query, city: j.city, country: j.country, org: j.org, timezone: j.timezone } });
            return;
          }
        }
      } catch (e) {}
      try {
        const r2 = await fetch('https://api.ip.sb/geoip', { signal: AbortSignal.timeout(10000) });
        if (r2.ok) {
          const j = await r2.json();
          if (j && j.ip) {
            sendResponse({ ok: true, data: { ip: j.ip, city: j.city, country: j.country, org: j.isp || j.organization, timezone: j.timezone } });
            return;
          }
        }
      } catch (e) {}
      sendResponse({ ok: false });
    })();
    return true; // 异步响应
  }
  if (msg && msg.type === 'FIX_TZ') {
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    if (!tabId) { sendResponse({ ok: false, err: '无法定位标签页' }); return; }
    // 幂等：已 attach 且同时区 → 直接成功（document_start 阶段每次进站都会调用，避免重复 attach/set）
    if (attached.tabId === tabId && attached.tz === msg.tz) {
      sendResponse({ ok: true, tz: msg.tz, cached: true });
      return;
    }
    (async () => {
      try {
        await attach(tabId);
        await sendCmd(tabId, 'Emulation.setTimezoneOverride', { timezoneId: msg.tz });
        attached = { tabId, tz: msg.tz };
        // 记录到 storage（popup/后台可查）
        await chrome.storage.session.set({ tzState: { tabId, tz: msg.tz, at: Date.now() } });
        // 持久缓存期望时区：下次进站 document_start 阶段提前预改（抢在 OpenAI 前端采集指纹之前）
        await chrome.storage.local.set({ lastTz: msg.tz });
        sendResponse({ ok: true, tz: msg.tz });
      } catch (e) {
        sendResponse({ ok: false, err: e.message });
      }
    })();
    return true; // 异步响应
  }

  if (msg && msg.type === 'GET_TZ_STATE') {
    chrome.storage.session.get(['tzState'], (s) => sendResponse(s.tzState || null));
    return true;
  }

  if (msg && msg.type === 'RESET_TZ') {
    // 恢复系统时区（detach 后自动还原）；同时清掉预改缓存，避免下次进站又自动改回
    (async () => {
      try {
        if (attached.tabId != null) {
          await chrome.debugger.detach({ tabId: attached.tabId }, () => {});
        }
        attached = { tabId: null, tz: null };
        await chrome.storage.session.remove(['tzState']);
        await chrome.storage.local.remove(['lastTz']);
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, err: e.message });
      }
    })();
    return true;
  }
});
