"""bili_viewer 网页接入面板：让 YUI / 用户通过浏览器网页完成浏览器接入与 B站绑定。

本地 HTTP 服务（127.0.0.1，零第三方依赖），懒启动（首次调用 bili_bind_web 时）。
页面上显示接入/绑定状态，并提供“一键接入”按钮：用户点击即授权插件尝试以
调试模式启动浏览器并读取 B站登录态——完全绕开本地进程状态判断，
残留进程（crashpad/后台 chrome.exe 等）不再阻挡接入。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_logger = logging.getLogger("N.E.K.O.Plugin_bili_viewer.plugin.bili_viewer")

_PANEL_HOST = "127.0.0.1"
_PANEL_PORT = 8765

_EXT_ONLINE_WINDOW = 120.0  # 扩展最近一次推送超过该秒数视为离线
_PANEL_URL = f"http://{_PANEL_HOST}:{_PANEL_PORT}/"

# 扩展通道请求体上限（字节）：截屏 base64（4K 屏 JPEG≈几 MB）与页面 DOM 实况
# 远大于默认 64KB，按端点分别放宽；同时防止无上限的磁盘/内存滥用。
_EXT_SHOT_MAX_BYTES = 16 * 1024 * 1024
_EXT_AUDIO_MAX_BYTES = 8 * 1024 * 1024  # 扩展声音分段（wav base64）上限：8s/16k/16bit 约 256KB，放宽防丢段
_EXT_DOM_MAX_BYTES = 2 * 1024 * 1024

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
  <div class="row"><span>写权限(CSRF)</span><span id="csrf" class="warn">—</span></div>
  <div class="row"><span>登录账号</span><span id="uname">—</span></div>
  <div class="row"><span>当前标签</span><span id="curtab" class="tip">—</span></div>
</div>
<button id="btn" onclick="doBind()">一键接入（启动调试浏览器并读取 B站登录态）</button>
<div class="tip">说明：接入不会关闭/删除你的浏览器，全程无需退出浏览器。若默认浏览器正被占用无法再开调试实例，
优先使用"B站接入桥"扩展通道（扩展随浏览器自动同步登录态），或刷新本页查看扩展通道状态即可。</div>
<div id="log"></div>
<script>
async function refresh(){
  try{
    const r = await fetch('/api/status'); const s = await r.json();
    document.getElementById('browser').textContent = s.browser || '未接入';
    document.getElementById('browser').className = s.browser_ok ? 'ok' : 'warn';
    document.getElementById('bili').textContent = s.bound ? '已绑定（自动接入）' : (s.cookie_manual ? '已绑定（手动配置）' : '未绑定');
    document.getElementById('bili').className = s.bound || s.cookie_manual ? 'ok' : 'bad';
    const c = document.getElementById('csrf');
    if (s.has_csrf === true) { c.textContent = '就绪（可发弹幕）'; c.className = 'ok'; }
    else if (s.has_csrf === false) { c.textContent = '缺失，需重新登录 B站'; c.className = 'bad'; }
    else { c.textContent = '—'; c.className = 'warn'; }
    document.getElementById('uname').textContent = s.uname || '—';
    const t = s.current_tab && s.current_tab.url;
    document.getElementById('curtab').textContent = t ? (s.current_tab.title || s.current_tab.url).slice(0, 40) : '—';
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

    def _read_json_body(self, max_len: int = 65536) -> dict:
        """读取并解析 JSON 请求体；超过 max_len 直接视为空（防内存/磁盘滥用）。

        extension-shot 通道（浏览器截屏 base64）与 extension-dom（页面 DOM 实况）
        的负载可能远超默认 64KB，调用方按端点传入更大上限；其余端点保持默认小上限。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > max_len:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return {}

    def _origin_allowed(self) -> bool:
        """来源校验：只接受本机面板、浏览器扩展与无来源（本地进程/curl）。

        恶意网页可通过 text/plain 的简单请求把伪造 cookie/tab 推送到本机
        127.0.0.1:8765（CSRF 式覆盖绑定），因此有 Origin/Referer 时必须放行
        可信来源，其余一律拒绝。
        """
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin:
            panel_origin = f"http://{_PANEL_HOST}:{self.server.server_address[1]}"
            if origin == panel_origin:
                return True
            if origin.startswith("chrome-extension://") or origin.startswith("edge-extension://"):
                return True
            return False
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            if referer.startswith(f"http://{_PANEL_HOST}:{self.server.server_address[1]}/"):
                return True
            if referer.startswith("chrome-extension://") or referer.startswith("edge-extension://"):
                return True
            return False
        return True  # 无来源（本地进程/脚本）视为可信

    # -- routes --
    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._json(403, {"error": "origin not allowed"})
            return
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
        if not self._origin_allowed():
            self._json(403, {"error": "origin not allowed"})
            return
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
                payload = dict(
                    self.server.ext_fn(
                        body.get("cookie", ""),
                        int(body.get("at", 0) or 0),
                        str(body.get("source", "") or ""),
                    )
                    or {}
                )
                _logger.info("ext cookie: cookie_len=%s at=%s source=%s ok=%s", len(str(body.get("cookie", "") or "")), body.get("at"), body.get("source"), payload.get("ok"))
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展推送处理异常：{exc}"}
                _logger.info("ext cookie: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-tab"):
            body = self._read_json_body()
            try:
                payload = dict(
                    self.server.tab_fn(
                        str(body.get("url", "") or ""),
                        str(body.get("title", "") or ""),
                        int(body.get("at", 0) or 0),
                        str(body.get("source", "") or ""),
                    )
                    or {}
                )
                _logger.info("ext tab: url=%s title=%s source=%s", str(body.get("url", "") or "")[:100], str(body.get("title", "") or "")[:40], body.get("source"))
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展标签页推送处理异常：{exc}"}
                _logger.info("ext tab: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-dom"):
            body = self._read_json_body(_EXT_DOM_MAX_BYTES)
            try:
                payload = dict(self.server.dom_fn(body) or {"ok": False})
                _logger.info("ext dom: kind=%s title=%s body_len=%s", body.get("kind"), str(body.get("title", "") or "")[:40], len(body))
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展页面DOM推送处理异常：{exc}"}
                _logger.info("ext dom: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-shot"):
            body = self._read_json_body(_EXT_SHOT_MAX_BYTES)
            try:
                payload = dict(self.server.shot_fn(body) or {"ok": False})
                _logger.info("ext shot: img_len=%s url=%s", len(str(body.get("image", "") or "")), str(body.get("url", "") or "")[:80])
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展截屏推送处理异常：{exc}"}
                _logger.info("ext shot: ERROR %s", exc)
            self._json(200, payload)
            return
        # 注意：/api/extension-audio-state 必须先于 /api/extension-audio 判断，
        # 否则会被 audio 前缀匹配吞掉（audio-state 请求体没有 base64 声音字段）。
        if self.path.startswith("/api/extension-audio-state"):
            body = self._read_json_body(64 * 1024)
            try:
                payload = dict(self.server.audio_state_fn(body) or {"ok": False})
                _logger.info("ext audio-state: enabled=%s at=%s ok=%s", body.get("enabled"), body.get("at"), payload.get("ok"))
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展声音状态推送处理异常：{exc}"}
                _logger.info("ext audio-state: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-audio"):
            body = self._read_json_body(_EXT_AUDIO_MAX_BYTES)
            try:
                payload = dict(self.server.audio_fn(body) or {"ok": False})
                _logger.info("ext audio: body_len=%s seq=%s ok=%s", len(body), body.get("seq"), payload.get("ok"))
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展声音推送处理异常：{exc}"}
                _logger.info("ext audio: ERROR %s", exc)
            self._json(200, payload)
            return
        self._json(404, {"error": "not found"})


class BindPanel:
    """懒启动的单例面板：ensure_started() 返回 (url, started_now)。

    port 可覆盖（默认 8765），测试场景传入随机端口避免冲突。
    """

    def __init__(self, status_fn, bind_fn, ext_fn=None, tab_fn=None, dom_fn=None, shot_fn=None, audio_fn=None, audio_state_fn=None, port: int = _PANEL_PORT) -> None:
        self._status_fn = status_fn
        self._bind_fn = bind_fn
        self._ext_fn = ext_fn or (lambda _cookie, _at: {"ok": False, "detail": "扩展通道未启用"})
        self._tab_fn = tab_fn or (lambda _url, _title, _at: {"ok": True})
        self._dom_fn = dom_fn or (lambda _body: {"ok": False, "detail": "扩展DOM通道未启用"})
        self._shot_fn = shot_fn or (lambda _body: {"ok": False, "detail": "扩展截屏通道未启用"})
        self._audio_fn = audio_fn or (lambda _body: {"ok": False, "detail": "扩展声音通道未启用"})
        self._audio_state_fn = audio_state_fn or (lambda _body: {"ok": False, "detail": "扩展声音状态通道未启用"})
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
            server.tab_fn = self._tab_fn
            server.dom_fn = self._dom_fn
            server.shot_fn = self._shot_fn
            server.audio_fn = self._audio_fn
            server.audio_state_fn = self._audio_state_fn
            thread = threading.Thread(target=server.serve_forever, name="bili-bind-panel", daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            return self.url(), True

    def url(self) -> str:
        return f"http://{_PANEL_HOST}:{self._port}/"

    def stop(self) -> None:
        """关闭面板服务并释放端口（插件重载/关闭时调用，避免旧线程占住 8765）。"""
        with self._lock:
            server, self._server = self._server, None
            self._thread = None
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    pass
                try:
                    server.server_close()
                except Exception:
                    pass

    @property
    def running(self) -> bool:
        return self._server is not None
