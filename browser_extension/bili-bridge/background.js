// B站接入桥 (bili-bridge) v0.2.1
// 1) 把 B站登录态直连推送给本机 bili_viewer 插件（POST 127.0.0.1:8765，不上外网）；
// 2) 把当前活动标签页（URL+标题）实时推送给插件，让 YUI 精确感知你在看什么。
// 触发时机：安装/浏览器启动立即推送；cookie 变化（onChanged 防抖）；
// 标签页切换/URL 变化（tabs 监听防抖）；alarms 30 秒兜底（MV3 SW 休眠时由 alarm 唤醒）；
// 推送失败 4 秒快速重试（插件重启后 5 秒内即可恢复感知，不再等下一轮 alarm）。

const PANEL_URL = "http://127.0.0.1:8765";

// 与插件 cdp_cookie._KEEP_NAMES 保持一致的 B站凭证名
const KEEP_NAMES = new Set([
  "SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
  "buvid3", "buvid4", "b_nut", "CURRENT_FNVAL",
  "bili_ticket", "bili_ticket_expires", "sid",
  "fingerprint", "fingerprintp", "buvid_fp",
]);

let pushTimer = null;
let retryTimer = null;
let retryCount = 0;

function buildCookieString(cookies) {
  const seen = new Set();
  const items = [];
  for (const c of cookies) {
    if (!c.domain.includes("bilibili")) continue;
    if (!KEEP_NAMES.has(c.name) || !c.value || seen.has(c.name)) continue;
    seen.add(c.name);
    items.push(`${c.name}=${c.value}`);
  }
  return items.join("; ");
}

// 推送失败后 4 秒快速重试（最多约 80 秒），覆盖插件/面板重启的空窗期
function scheduleRetry() {
  clearTimeout(retryTimer);
  if (retryCount > 20) return;
  retryCount += 1;
  retryTimer = setTimeout(() => { pushCookie(); pushTab(); }, 4000);
}

async function postJson(path, body) {
  try {
    const resp = await fetch(`${PANEL_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (resp && resp.ok) retryCount = 0; // 任一路径成功即视为链路恢复
    return resp;
  } catch (e) {
    // 插件未运行/本地面板未启动：静默，快速重试兜底
    scheduleRetry();
    return null;
  }
}

async function pushCookie() {
  try {
    const cookies = await chrome.cookies.getAll({ domain: "bilibili.com" });
    const cookie = buildCookieString(cookies);
    await postJson("/api/extension-cookie", {
      cookie, at: Date.now(), source: "bili-bridge-extension",
    });
  } catch (e) { scheduleRetry(); }
}

async function pushTab() {
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const tab = tabs && tabs[0];
    if (!tab || !tab.url || tab.url.startsWith("chrome://") || tab.url.startsWith("edge://")) return;
    await postJson("/api/extension-tab", {
      url: tab.url, title: tab.title || "", at: Date.now(),
    });
  } catch (e) { scheduleRetry(); }
}

function scheduleCookie() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => { pushCookie(); }, 800);
}

function scheduleTab() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => { pushTab(); }, 800);
}

chrome.cookies.onChanged.addListener((changeInfo) => {
  const c = changeInfo && changeInfo.cookie;
  if (c && c.domain && c.domain.includes("bilibili")) scheduleCookie();
});

// 标签页切换 / URL 变化：防抖后推送当前活动标签页
chrome.tabs.onActivated.addListener(() => scheduleTab());
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url || changeInfo.title) scheduleTab();
});

chrome.alarms.create("bili-bridge-push", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bili-bridge-push") { pushCookie(); pushTab(); }
});

chrome.runtime.onInstalled.addListener(() => {
  pushCookie();
  pushTab();
});

// 浏览器/扩展启动时立即推送一次
pushCookie();
pushTab();