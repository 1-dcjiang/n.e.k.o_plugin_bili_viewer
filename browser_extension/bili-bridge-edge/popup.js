// popup 状态展示：读取插件面板的扩展状态端点
const PANEL_URL = "http://127.0.0.1:8765";

async function refresh() {
  const st = document.getElementById("st");
  const at = document.getElementById("at");
  const tab = document.getElementById("tab");
  try {
    const r = await fetch(`${PANEL_URL}/api/extension-status`);
    const s = await r.json();
    if (s.online) {
      st.textContent = "运行中（已连插件）";
      st.className = "ok";
      const d = s.last_seen ? new Date(s.last_seen).toLocaleTimeString() : "刚刚";
      at.textContent = d;
      if (s.current_tab && s.current_tab.url) {
        const t = s.current_tab.title || s.current_tab.url;
        tab.textContent = t.length > 26 ? t.slice(0, 25) + "…" : t;
        tab.title = s.current_tab.url;
      } else { tab.textContent = "—"; }
    } else {
      st.textContent = "插件未运行或未收到推送";
      st.className = "bad";
      at.textContent = s.last_seen ? new Date(s.last_seen).toLocaleTimeString() : "—";
    }
  } catch (e) {
    st.textContent = "插件面板未运行";
    st.className = "bad";
    at.textContent = "—";
  }
}

refresh();
