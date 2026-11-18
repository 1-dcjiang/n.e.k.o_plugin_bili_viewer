"""bili_viewer 网页接入面板：让 YUI / 用户通过浏览器网页完成浏览器接入与 B站绑定。

本地 HTTP 服务（127.0.0.1，零第三方依赖），懒启动（首次调用 bili_bind_web 时）。
页面上显示接入/绑定状态，并提供“一键接入”按钮：用户点击即授权插件尝试以
调试模式启动浏览器并读取 B站登录态——完全绕开本地进程状态判断，
残留进程（crashpad/后台 chrome.exe 等）不再阻挡接入。
"""

from __future__ import annotations

import collections
import json
import logging
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_logger = logging.getLogger("N.E.K.O.Plugin_bili_viewer.plugin.bili_viewer")

_PANEL_HOST = "127.0.0.1"
_PANEL_PORT = 8765
_PANEL_TOKEN_HEADER = "X-Neko-Token"
_PANEL_COOKIE = "neko_panel_session"
# v0.9.38：三个扩展的 manifest 共用固定公钥，Origin 只能是这个扩展 ID。
# 旧版任意 chrome-extension:// 放行等于把本机写接口交给任意扩展，必须收紧。
_EXTENSION_ID = "dafbbcelkodnlgmpjdaabcblddgicjhp"
_EXTENSION_ORIGIN = f"chrome-extension://{_EXTENSION_ID}"
_EXTENSION_API_PREFIXES = ("/api/extension-", "/api/pending-command")

_EXT_ONLINE_WINDOW = 120.0  # 扩展最近一次推送超过该秒数视为离线
_PANEL_URL = f"http://{_PANEL_HOST}:{_PANEL_PORT}/"

# 扩展通道请求体上限（字节）：截屏 base64（4K 屏 JPEG≈几 MB）与页面 DOM 实况
# 远大于默认 64KB，按端点分别放宽；同时防止无上限的磁盘/内存滥用。
_EXT_SHOT_MAX_BYTES = 16 * 1024 * 1024
_EXT_AUDIO_MAX_BYTES = 8 * 1024 * 1024  # 扩展声音分段（wav base64）上限：8s/16k/16bit 约 256KB，放宽防丢段
_EXT_DOM_MAX_BYTES = 2 * 1024 * 1024

# v0.2.17：扩展采集的直播弹幕缓冲 + @小号通知（YUI 实时看到弹幕、被@时提醒）
# v0.8.6：缓冲条目带房间号 (sender, text, at, room)——供"直播间活跃观众"提取
_DANMAKU_BUF: collections.deque = collections.deque(maxlen=120)  # (sender, text, at, room)
_MENTION_NOTIFY: dict = {"at": 0.0, "text": "", "sender": "", "room": ""}
_YUI_NICKNAMES = ("yui小猫娘", "小猫娘", "yui")  # 弹幕 @ 匹配昵称（小号默认昵称）

# v0.7.35：扩展指令队列——插件工具把"进入直播间"等命令写入，扩展 alarm 轮询拉取执行
_CMD_QUEUE: list = []          # [{id, type, room, created}]
_CMD_SEQ: int = 0
_CMD_LOCK = threading.Lock()


# v0.9.44：v0.2.18 那套"扩展推送只保留核心字段、按 uid 自助刷新"已整体下线——
# v0.9.40 起由 __init__._assign_cookie_locked 在验真通过后整份归位，web_ui 侧
# 只保留 HTTP 端点转发，不再持有任何 Cookie 定位/裁剪/写盘逻辑。


def get_recent_danmaku(limit: int = 40) -> list:
    """最近弹幕（供 __init__.py 实时读取）：[(sender, text, at), ...]"""
    return [(s, t, a) for s, t, a, _r in list(_DANMAKU_BUF)[-limit:]]


def get_recent_room(max_age: float = _EXT_ONLINE_WINDOW) -> str:
    """弹幕 WS 流最近的房间号（v0.8.6：页面真实直播房间的权威来源）。

    WS 弹幕连接绑定页面实际直播间，每来一条弹幕就刷新一条带 room 的记录——
    比"扩展标签页事件"可靠（后者只在切换/加载标签时推送一次，长时间停留会过期，
    感知回退窗口标题搜索就可能定位错房间）。返回 '' 表示没有新鲜弹幕房间。
    """
    now = time.time()
    for _s, _t, at, room in reversed(_DANMAKU_BUF):
        r = str(room or "").strip()
        if r and now - float(at or 0) <= max_age:
            return r
    return ""


def get_recent_viewers(room: str = "", limit: int = 50) -> list[str]:
    """最近弹幕发言者昵称去重（v0.8.6：直播间活跃观众近似来源）。

    B站 无公开 HTTP 接口返回"当前在线观众"（页面数字来自 WS），
    弹幕发言者是最可靠的可关注对象。room 非空时只统计该房间；
    排除空昵称与系统提示。按最近发言顺序（新→旧）去重。
    """
    seen: set[str] = set()
    out: list[str] = []
    room = (room or "").strip()
    for sender, _t, _a, r in reversed(_DANMAKU_BUF):
        s = str(sender or "").strip()
        if not s or s == "系统提示" or s == "系统":
            continue
        if room and str(r or "").strip() != room:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def get_mention_notify(clear: bool = True) -> dict:
    """最近一条 @小号 的通知（供感知循环读取后消费）"""
    info = dict(_MENTION_NOTIFY)
    if clear:
        _MENTION_NOTIFY["text"] = ""
        _MENTION_NOTIFY["sender"] = ""
        _MENTION_NOTIFY["room"] = ""
    return info

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
  <div class="row"><span>当前标签</span><span id="curtab" class="tip">—</span></div>
</div>
<button id="btn" onclick="doBind()">一键接入（启动调试浏览器并读取 B站登录态）</button>
<div class="tip">说明：接入不会关闭/删除你的浏览器，全程无需退出浏览器。若默认浏览器正被占用无法再开调试实例，
优先使用"B站接入桥"扩展通道（扩展随浏览器自动同步登录态），或刷新本页查看扩展通道状态即可。</div>
<div id="log"></div>
<script>
const AUTH_HEADERS = {};  // v0.9.40：面板改 HttpOnly 会话 Cookie，页面里不再内嵌 token
async function refresh(){
  try{
    const r = await fetch('/api/status', {headers: AUTH_HEADERS}); const s = await r.json();
    document.getElementById('browser').textContent = s.browser || '未接入';
    document.getElementById('browser').className = s.browser_ok ? 'ok' : 'warn';
    document.getElementById('bili').textContent = s.bound ? '已绑定（自动接入）' : (s.cookie_manual ? '已绑定（手动配置）' : '未绑定');
    document.getElementById('bili').className = s.bound || s.cookie_manual ? 'ok' : 'bad';
    const c = document.getElementById('csrf');
    if (s.has_csrf === true) { c.textContent = '就绪（可发弹幕）'; c.className = 'ok'; }
    else if (s.has_csrf === false) { c.textContent = '缺失，需重新登录 B站'; c.className = 'bad'; }
    else { c.textContent = '—'; c.className = 'warn'; }
    const t = s.current_tab && s.current_tab.url;
    document.getElementById('curtab').textContent = t ? (s.current_tab.title || s.current_tab.url).slice(0, 40) : '—';
  }catch(e){ document.getElementById('log').textContent = '状态刷新失败：' + e; }
}
async function doBind(){
  const btn = document.getElementById('btn'); btn.disabled = true;
  document.getElementById('log').textContent = '正在接入…（启动调试浏览器，最多约 10 秒）';
  try{
    const r = await fetch('/api/bind', {method:'POST', headers: AUTH_HEADERS}); const s = await r.json();
    document.getElementById('log').textContent = s.detail || JSON.stringify(s);
  }catch(e){ document.getElementById('log').textContent = '接入失败：' + e; }
  btn.disabled = false; refresh();
}
setInterval(refresh, 3000); refresh();
</script>
</body>
</html>
"""


def _secure_equal(supplied: object, expected: object) -> bool:
    """统一 UTF-8 bytes 比较，非 ASCII token 只返回 False，不抛 TypeError。"""
    return secrets.compare_digest(str(supplied).encode("utf-8"), str(expected).encode("utf-8"))


class _PanelHandler(BaseHTTPRequestHandler):
    """面板 HTTP 处理器；绑定/扩展回调由 BindPanel 挂到 server 实例属性上注入。"""

    def log_message(self, *_args):  # 静默默认访问日志
        pass

    # -- helpers --
    def _send(self, code: int, ctype: str, body: bytes, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _read_json_body(self, max_len: int = 65536) -> dict:
        """读取并解析 JSON；超限返回空对象，并有界排空请求体避免 Windows RST。

        超限不分配与声明长度等大的缓冲区，只用小块读取并丢弃。极端大的请求
        不继续消耗带宽，直接关闭连接；正常的略超限扩展请求仍能收到完整响应，
        不会因服务端带着未读请求体关闭 socket 而让客户端拿到 WinError 10053。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > max_len:
                if length <= _EXT_SHOT_MAX_BYTES:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 16 * 1024))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                else:
                    self.close_connection = True
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return {}

    def _origin_kind(self) -> str:
        """把请求来源分类为 panel / extension / none / bad（v0.9.38）。

        只认本面板 Origin 与固定扩展 ID，不再放行任意 chrome-extension://；
        无 Origin/Referer 的本地进程请求保留，但写操作必须再带面板 token。
        """
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        port = self.server.server_address[1]
        panel_origin = f"http://{_PANEL_HOST}:{port}"
        if origin:
            if origin == panel_origin:
                return "panel"
            if origin == _EXTENSION_ORIGIN:
                return "extension"
            return "bad"
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            if referer == panel_origin or referer.startswith(panel_origin + "/"):
                return "panel"
            if referer == _EXTENSION_ORIGIN or referer.startswith(_EXTENSION_ORIGIN + "/"):
                return "extension"
            return "bad"
        return "none"

    def _request_trusted(self, allow_extension: bool = False, require_token: bool = False) -> bool:
        """统一来源 + 写操作 token 校验；扩展只允许访问扩展专属端点。"""
        kind = self._origin_kind()
        if kind == "bad":
            return False
        if kind == "extension" and not allow_extension:
            return False
        if require_token and kind != "extension" and not self._queue_token_ok():
            return False
        return True

    def _host_allowed(self) -> bool:
        """Host 校验：只接受本机面板地址，阻断 DNS rebinding（v0.9.37）。

        攻击者把恶意域名解析到 127.0.0.1 时，浏览器发出的 Host 仍是恶意域名；
        仅做 Origin 校验会漏掉“无 Origin”的简单请求/导航，因此必须同时校验 Host。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _queue_token_ok(self) -> bool:
        """写操作鉴权：面板 token，可用 HttpOnly 会话 Cookie 或 X-Neko-Token 头（v0.9.40）。

        v0.9.38 把 token 直接替换进 GET / 的 HTML，任何本机进程 GET / 就能拿到；
        v0.9.40 改成：GET / 必须带 bind 时返回的 bootstrap token（?k=）才会下发
        HttpOnly 会话 Cookie；页面 JS 不再持有 token，Cookie 也不会被 JS 读取。
        """
        expected = str(getattr(self.server, "queue_token", "") or "")
        if not expected:
            return False
        supplied = str(self.headers.get(_PANEL_TOKEN_HEADER) or "")
        if supplied and _secure_equal(supplied, expected):
            return True
        for part in str(self.headers.get("Cookie") or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == _PANEL_COOKIE and _secure_equal(value, expected):
                return True
        return False

    def _bootstrap_ok(self, supplied: str) -> bool:
        expected = str(getattr(self.server, "bootstrap_token", "") or "")
        return bool(expected) and bool(supplied) and _secure_equal(supplied, expected)

    def _extension_path(self) -> bool:
        return self.path.startswith(_EXTENSION_API_PREFIXES)

    # -- routes --
    def do_GET(self) -> None:
        if not self._host_allowed() or self._origin_kind() == "bad":
            self._json(403, {"error": "host/origin not allowed"})
            return
        status_fn = self.server.status_fn
        if self.path.startswith("/api/status"):
            if self._origin_kind() == "extension":
                self._json(403, {"error": "extension cannot read panel status"})
                return
            # v0.9.40：状态接口也要求鉴权，本机任意进程不能再直接读面板状态
            if not self._request_trusted(require_token=True):
                self._json(403, {"error": "panel token required"})
                return
            payload = dict(status_fn() or {})
            payload.pop("uname", None)  # 账号名/uid 不上面板接口（v0.9.38）
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
        if self.path.startswith("/api/pending-command"):
            if not self._request_trusted(allow_extension=True, require_token=True):
                self._json(403, {"error": "queue token required"})
                return
            with _CMD_LOCK:
                now = time.time()
                _CMD_QUEUE[:] = [c for c in _CMD_QUEUE if now - (c.get("created") or now) < 600]
                cmd = _CMD_QUEUE[0] if _CMD_QUEUE else {}
            self._json(200, cmd or {"id": None})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            supplied = (urllib.parse.parse_qs(parsed.query).get("k") or [""])[0]
            has_session = self._queue_token_ok()
            if not has_session and not self._bootstrap_ok(supplied):
                self._json(403, {"error": "panel token required"})
                return
            headers = {}
            if not has_session:
                # 只在 bootstrap token 正确时下发会话 Cookie；token 本身不出现在 HTML 里
                headers["Set-Cookie"] = (
                    f"{_PANEL_COOKIE}={self.server.queue_token}; HttpOnly; SameSite=Strict; Path=/"
                )
            self._send(
                200,
                "text/html; charset=utf-8",
                _PAGE.encode("utf-8"),
                headers=headers,
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "host/origin not allowed"})
            return
        if not self._request_trusted(allow_extension=self._extension_path(), require_token=True):
            self._json(403, {"error": "queue token required"})
            return
        if self.path.startswith("/api/bind"):
            try:
                payload = dict(self.server.bind_fn() or {})
            except Exception as exc:  # 绑定动作绝不让面板崩溃
                payload = {"ok": False, "detail": f"接入异常：{exc}"}
            self._json(200, payload)
            return
        if self.path.startswith("/api/queue-command"):
            if not self._queue_token_ok():
                self._json(403, {"error": "queue token required"})
                return
            global _CMD_SEQ
            body = self._read_json_body()
            cmd_type = body.get("type") or ""
            room = body.get("room") or ""
            browser = str(body.get("browser") or "").strip().lower()
            if cmd_type == "enter-live" and str(room).isdigit():
                with _CMD_LOCK:
                    _CMD_SEQ += 1
                    _CMD_QUEUE.append({"id": _CMD_SEQ, "type": "enter-live", "room": str(room),
                                       "browser": browser or None, "created": time.time()})
                    cmd_id = _CMD_SEQ
                self._json(200, {"queued": True, "id": cmd_id})
            else:
                self._json(400, {"error": "bad command"})
            return
        if self.path.startswith("/api/pending-command"):
            body = self._read_json_body()
            ack = body.get("ack")
            if ack is not None:
                with _CMD_LOCK:
                    _CMD_QUEUE[:] = [c for c in _CMD_QUEUE if c.get("id") != ack]
            self._json(200, {"ok": True})
            return
        if self.path.startswith("/api/extension-cookie"):
            body = self._read_json_body()
            cookie = str(body.get("cookie", "") or "")
            try:
                # v0.9.40：先验真/归位，只有 nav 验真通过并成功归位后才刷新 cookie 文件。
                # 旧顺序先 _refresh_ext_cookie 再 ext_fn，会让伪造 Cookie 在验真失败前
                # 就覆盖真实登录态。
                payload = dict(
                    self.server.ext_fn(
                        cookie,
                        int(body.get("at", 0) or 0),
                        str(body.get("source", "") or ""),
                    )
                    or {}
                )
                # _assign_cookie_locked 已按权威角色写入完整 Cookie，不再二次
                # _refresh_ext_cookie；旧实现的裁剪版重写在另一个锁域重复写同一文件。
                # 只有验真通过且归位成功才继续（payload 已由 ext_fn 返回）。
                _logger.info("ext cookie: cookie_len=%s at=%s source=%s ok=%s", len(cookie), body.get("at"), body.get("source"), payload.get("ok"))
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
                _logger.debug(
                    "ext tab: url_len=%s title_len=%s is_bili=%s source=%s",
                    len(str(body.get("url", "") or "")),
                    len(str(body.get("title", "") or "")),
                    payload.get("is_bili"),
                    body.get("source"),
                )
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展标签页推送处理异常：{exc}"}
                _logger.info("ext tab: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-dom"):
            body = self._read_json_body(_EXT_DOM_MAX_BYTES)
            try:
                payload = dict(self.server.dom_fn(body) or {"ok": False})
                _logger.debug(
                    "ext dom: kind=%s title_len=%s body_len=%s is_bili=%s",
                    body.get("kind"),
                    len(str(body.get("title", "") or "")),
                    len(body),
                    payload.get("is_bili"),
                )
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展页面DOM推送处理异常：{exc}"}
                _logger.info("ext dom: ERROR %s", exc)
            self._json(200, payload)
            return
        if self.path.startswith("/api/extension-shot"):
            body = self._read_json_body(_EXT_SHOT_MAX_BYTES)
            try:
                payload = dict(self.server.shot_fn(body) or {"ok": False})
                _logger.debug(
                    "ext shot: img_len=%s url_len=%s is_bili=%s",
                    len(str(body.get("image", "") or "")),
                    len(str(body.get("url", "") or "")),
                    payload.get("is_bili"),
                )
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
                _logger.info(
                    "ext audio-state: enabled=%s at=%s ok=%s ctx_state=%s ctx_err=%s",
                    body.get("enabled"), body.get("at"), payload.get("ok"),
                    body.get("ctx_state"), str(body.get("ctx_err") or "")[:200],
                )
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
        if self.path.startswith("/api/extension-danmaku"):
            body = self._read_json_body(64 * 1024)
            try:
                text = str(body.get("text", "") or "")[:200]
                sender = str(body.get("sender", "") or "")[:40]
                room = str(body.get("room", "") or "")[:20]
                if text:
                    _DANMAKU_BUF.append((sender, text, int(body.get("at") or 0) or time.time(), room))
                    mentioned = any(n in text.lower() for n in _YUI_NICKNAMES)
                    if mentioned:
                        _MENTION_NOTIFY["at"] = time.time()
                        _MENTION_NOTIFY["text"] = text
                        _MENTION_NOTIFY["sender"] = sender
                        _MENTION_NOTIFY["room"] = room
                    # INFO 只留元数据：弹幕原文与发送者昵称不进入持久日志。
                    _logger.info("ext danmaku: room=%s mention=%s text_len=%d", room, mentioned, len(text))
                payload = {"ok": True}
            except Exception as exc:
                payload = {"ok": False, "detail": f"扩展弹幕推送处理异常：{exc}"}
                _logger.info("ext danmaku: ERROR %s", exc)
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
        # 面板启动时生成两个 token：
        # - panel_token：HttpOnly 会话 Cookie / X-Neko-Token 头，只有插件工具和已 bootstrap 的页面能用；
        # - bootstrap_token：只出现在插件工具返回的打开链接里，GET / 凭它换会话 Cookie。
        # v0.9.40：不再把 panel_token 内嵌进 GET / 的 HTML（旧版本本机任意进程 GET / 就能拿到）。
        self._panel_token = secrets.token_urlsafe(24)
        self._bootstrap_token = secrets.token_urlsafe(32)

    @property
    def token(self) -> str:
        return self._panel_token

    def ensure_started(self) -> tuple[str, bool]:
        with self._lock:
            if self._server is not None:
                return self.url(), False
            server = ThreadingHTTPServer((_PANEL_HOST, self._port), _PanelHandler)
            # port=0 时由系统分配；把实际端口写回，url()/page_url() 才能给出可访问地址
            self._port = int(server.server_address[1])
            # 回调挂到 server 实例属性（每请求 handler 通过 self.server 取，避免类属性 self 绑定）
            server.status_fn = self._status_fn
            server.bind_fn = self._bind_fn
            server.ext_fn = self._ext_fn
            server.tab_fn = self._tab_fn
            server.dom_fn = self._dom_fn
            server.shot_fn = self._shot_fn
            server.audio_fn = self._audio_fn
            server.audio_state_fn = self._audio_state_fn
            server.queue_token = self._panel_token
            server.bootstrap_token = self._bootstrap_token
            thread = threading.Thread(target=server.serve_forever, name="bili-bind-panel", daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
            return self.url(), True

    def url(self) -> str:
        return f"http://{_PANEL_HOST}:{self._port}/"

    def page_url(self) -> str:
        """给用户打开的页面地址：带 bootstrap token，用来换 HttpOnly 会话 Cookie。"""
        return f"{self.url()}?k={self._bootstrap_token}"

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
