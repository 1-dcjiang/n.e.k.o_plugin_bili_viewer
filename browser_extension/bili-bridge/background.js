// B站接入桥 (bili-bridge) — 把 B站登录态直连推送给本机 bili_viewer 插件。
// 仅 POST 到 127.0.0.1:8765（插件本地面板服务），Cookie 不经过任何外部网络。
// 触发时机：安装/浏览器启动立即推送一次；cookie 变化（onChanged 防抖）自动推送；
// alarms 每分钟兜底一次（MV3 service worker 休眠时由 alarm 唤醒）。

const PANEL_URL = "http://127.0.0.1:8765";

// 与插件 cdp_cookie._KEEP_NAMES 保持一致的 B站凭证名
const KEEP_NAMES = new Set([
  "SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
  "buvid3", "buvid4", "bili_ticket", "bili_ticket_expires",
  "b_nut", "b_lsid", "buvid_fp", "buvid_fp_plain", "buvid_fp_proxy",
  "sid", "CURRENT_FNVAL", "CURRENT_QUALITY",
  "fingerprint", "fingerprint3",
]);

let pushTimer = null;

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

async function pushCookie() {
  try {
    const cookies = await chrome.cookies.getAll({ domain: "bilibili.com" });
    const cookie = buildCookieString(cookies);
    if (!cookie.includes("SESSDATA=")) {
      // 未登录/登录态过期：告知插件（便于面板显示状态）
      await fetch(`${PANEL_URL}/api/extension-cookie`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookie: "", at: Date.now(), source: "bili-bridge-extension" }),
      });
      return;
    }
    await fetch(`${PANEL_URL}/api/extension-cookie`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie, at: Date.now(), source: "bili-bridge-extension" }),
    });
  } catch (e) {
    // 插件未运行/本地面板未启动：静默，下次定时/变化再试
  }
}

function schedulePush() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(pushCookie, 800);
}

chrome.cookies.onChanged.addListener((changeInfo) => {
  const c = changeInfo && changeInfo.cookie;
  if (c && c.domain && c.domain.includes("bilibili")) {
    schedulePush();
  }
});

chrome.alarms.create("bili-bridge-push", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bili-bridge-push") pushCookie();
});

chrome.runtime.onInstalled.addListener(() => {
  pushCookie();
});

// 浏览器/扩展启动时立即推送一次
pushCookie();
