"""bili_viewer 网页接入面板：让 YUI / 用户通过浏览器网页完成浏览器接入与 B站绑定。

本地 HTTP 服务（127.0.0.1，零第三方依赖），懒启动（首次调用 bili_bind_web 时）。
页面上显示接入/绑定状态，并提供“一键接入”按钮：用户点击即授权插件尝试以
调试模式启动浏览器并读取 B站登录态——完全绕开本地进程状态判断，
残留进程（crashpad/后台 chrome.exe 等）不再阻挡接入。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PANEL_HOST = "127.0.0.1"
_PANEL_PORT = 8765

_EXT_ONLINE_WINDOW = 120.0  # 扩展最近一次推送超过该秒数视为离线
_PANEL_URL = f"http://{_PANEL_HOST}:{_PANEL_PORT}/"

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>B站接入面板</title>
<style>
  body{font-family:'Microsoft YaHei',sans-serif;max-width:720px;margin:40px auto;padding:0 20px;background:#1a1a2e;color:#eee}
  h1{color:#8ecae6} .card{background:#232345;border-radius:12px;padding:20px;margin:16px 0}
  .row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #333}
  .row:last-child{border-bottom:none}
  .ok{color:#7ae582;font-weight:bold} .warn{color:#ffd166} .bad{color:#ef476f;font-weight:bold}
  button{background:#3a86ff;color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:16px;cursor:pointer;margin-top:8px}
  button:hover{background:#2f6fd6} button:disabled{background:#555;cursor:not-allowed}
  #log{font-size:13px;color:#aaa;white-space:pre-wrap;margin-top:8px}
  .tip{font-size:13px;color:#999}
</style>
</head>
<body>
<h1>🅱️ B站接入面板</h1>
<div class="card">
  <div class="row"><span>浏览器调试实例</span><span id="browser" class="warn">检测中…</span></div>
  <div class="row"><span>B站登录态</span><span id="bili" class="warn">检测中…</span></div>
  <div class="row"><span>登录账号</span><span id="uname">—</span></div>
</div>
<button id="btn" onclick="doBind()">一键接入（启动调试浏览器并读取 B站登录态）</button>
<div class="tip">说明：接入不会关闭/删除你的浏览器。若默认浏览器正被占用无法再开调试实例，
面板会提示先完全退出一次（结束 chrome.exe/msedge.exe，残留后台进程会一并退出），
然后回到这里再点一次即可。</div>
<div id="log"></div>
<script>
async function refresh(){
  try{
    const r = await fetch('/api/status'); const s = await r.json();
    document.getElementById('browser').textContent = s.browser || '未接入';
    document.getElementById('browser').className = s.browser_ok ? 'ok' : 'warn';
    document.getElementById('bili').textContent = s.bound ? '已绑定（自动接入）' : (s.cookie_manual ? '已绑定（手动配置）' : '未绑定');
    document.getElementById('bili').className = s.bound || s.cookie_manual ? 'ok' : 'bad';
    document.getElementById('uname').textContent = s.uname || '—';
  }catch(e){ document.getElementById('log').textContent = '状态刷新失败：' + e; }
}
async function doBind(){
  const btn = document.getElementById('btn'); btn.disabled = true;
  document.getElementById('log').textContent = '正在接入…（启动调试浏览器，最多约 10 秒）';
  try{
    const r = await fetch('/api/bind', {method:'POST'}); const s = await r.json();
    document.getElementById('log').textContent = s.detail || JSON.stringify(s);
  }catch(e){ document.getElementById('log').textContent = '接入失败：' + e; }
  btn.disabled = false; refresh();
}
setInterval(refresh, 3000); refresh();
</script>
</body>
</html>
"""


class _PanelHandler(BaseHTTPRequestHandler):
    """面板 HTTP 处理器；绑定/扩展回调由 BindPanel 挂到 server 实例属性上注入。"""

    def log_message(self, *_args):  # 静默默认访问日志
        pass

    # -- helpers --
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 65536:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return {}

    # -- routes --
    def do_GET(self) -> None:
        status_fn = self.server.status_fn
        if self.path.startswith("/api/status"):
            payload = dict(status_fn() or {})
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-status"):
            payload = dict(status_fn() or {})
            self._json(200, {
                "online": bool(payload.get("extension_online")),
                "last_seen": payload.get("extension_last_seen"),
                "bound": bool(payload.get("bound")),
            })
            return
        self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))

    def do_POST(self) -> None:
        if self.path.startswith("/api/bind"):
            try:
                payload = dict(self.server.bind_fn() or {})
            except Exception as exc:  # 绑定动作绝不让面板崩溃
                payload = {"ok": False, "detail": f"接入异常：{exc}"}
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-cookie"):
            body = self._read_json_body()
            try:
                payload = dict(self.server.ext_fn(body.get("cookie", ""), int(body.get("at", 0) or 0)) or {})
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展推送处理异常：{exc}"}
            self._json(200, payload)
            return
        self._json(404, {"error": "not found"})


class BindPanel:
    """懒启动的单例面板：ensure_started() 返回 (url, started_now)。

    port 可覆盖（默认 8765），测试场景传入随机端口避免冲突。
    """

    def __init__(self, status_fn, bind_fn, ext_fn=None, port: int = _PANEL_PORT) -> None:
        self._status_fn = status_fn
        self._bind_fn = bind_fn
        self._ext_fn = ext_fn or (lambda _cookie, _at: {"ok": False, "detail": "扩展通道未启用"})
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def ensure_started(self) -> tuple[str, bool]:
        with self._lock:
            if self._server is not None:
                return self.url(), False
            server = ThreadingHTTPServer((_PANEL_HOST, self._port), _PanelHandler)
            # 回调挂到 server 实例属性（每请求 handler 通过 self.server 取，避免类属性 self 绑定）
            server.status_fn = self._status_fn
            server.bind_fn = self._bind_fn
            server.ext_fn = self._ext_fn
            thread = threading.Thread(target=server.serve_forever, name="bili-bind-panel", daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            return self.url(), True

    def url(self) -> str:
        return f"http://{_PANEL_HOST}:{self._port}/"

    @property
    def running(self) -> bool:
        return self._server is not None
