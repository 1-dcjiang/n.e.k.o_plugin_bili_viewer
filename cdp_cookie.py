"""bili_viewer CDP 运行时接入：从正在运行的 Chrome/Edge 直接读取 B站 Cookie。

为什么需要它：Chrome 新版（2025+）启用 App-Bound Encryption，Cookie 用 v20 加密，
密钥绑定浏览器进程，离线文件解密不可行；而 CDP（DevTools 协议）让浏览器进程
自己解密并返回明文 Cookie——真正做到"接入正在使用的浏览器"，无需关闭浏览器。

实现要点：
- 零第三方依赖（urllib + socket + struct + base64 手写 WebSocket 客户端）；
- 优先连接已运行的调试实例；没有则按需以调试模式启动浏览器（复用原 profile）；
- 仅绑定 127.0.0.1，Cookie 不上传、不写日志；
- 浏览器保持正常运行（只是多开一个本地调试端口），用户无感。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request

_DEBUG_PORT_BASE = 9333
_DEBUG_PORT_MAX = 9333 + 20
_POPEN_DETACHED = 0x00000008 | 0x08000000

_BROWSER_EXE_PATHS: dict[str, tuple[str, ...]] = {
    "Chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ),
    "Edge": (
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
}

_BROWSER_DIRS: dict[str, str] = {
    "Chrome": os.path.join("Google", "Chrome", "User Data"),
    "Edge": os.path.join("Microsoft", "Edge", "User Data"),
}

_BROWSER_EXES: dict[str, str] = {"Chrome": "chrome.exe", "Edge": "msedge.exe"}

_STATUS_BOUND = "bound"
_STATUS_NOT_LOGGED_IN = "not_logged_in"
_STATUS_NO_BROWSER = "no_browser"
_STATUS_FAIL = "fail"

_KEEP_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
    "b_nut",
    "CURRENT_FNVAL",
    "bili_ticket",
    "bili_ticket_expires",
    "sid",
    "fingerprint",
    "fingerprintp",
    "buvid_fp",
)


# ══════════ 极简 WebSocket 客户端（仅客户端帧 + 文本） ══════════


def _ws_connect(ws_url: str, timeout: float = 10.0):
    u = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    parsed = urllib.parse.urlsplit(u)
    host, port = parsed.hostname, parsed.port or 80
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\nOrigin: http://localhost\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("ws handshake: conn closed")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise OSError("ws handshake failed: " + head[:200].decode(errors="replace"))
    if rest:
        sock.sendall(rest)
    return sock


def _ws_send(sock, payload: bytes) -> None:
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = b"\x81" + bytes([0x80 | n])
    elif n < 65536:
        header = b"\x81" + bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header = b"\x81" + bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def _ws_recv(sock, timeout: float = 15.0) -> bytes:
    sock.settimeout(timeout)

    def _read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise OSError("ws conn closed")
            buf += c
        return buf

    hdr = _read_exact(2)
    op = hdr[0] & 0x0F
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _read_exact(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _read_exact(8))[0]
    mask = _read_exact(4) if hdr[1] & 0x80 else None
    data = _read_exact(ln)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if op == 9:  # ping
        return _ws_recv(sock, timeout)
    if op == 8:
        raise OSError("ws closed by peer")
    if op == 2:
        raise OSError("ws binary frame unexpected")
    return data


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    sock = _ws_connect(ws_url)
    try:
        _ws_send(sock, json.dumps({"id": 1, "method": method, "params": params or {}}).encode())
        raw = b""
        while True:
            raw += _ws_recv(sock, timeout)
            try:
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    return msg
            except Exception:
                continue
    finally:
        sock.close()


# ══════════ 调试实例管理 ══════════


def _http_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        return json.load(urllib.request.urlopen(url, timeout=timeout))
    except Exception:
        return None


def _browser_running(exe_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=5,
            creationflags=_POPEN_DETACHED,
        )
        return exe_name.lower() in proc.stdout.lower()
    except Exception:
        return True


def _find_debug_port() -> int | None:
    for port in range(_DEBUG_PORT_BASE, _DEBUG_PORT_MAX):
        if _http_json(f"http://127.0.0.1:{port}/json/version") is not None:
            return port
    return None


def _launch_debug_browser(name: str) -> int | None:
    """以调试模式启动浏览器（复用原 profile），返回调试端口；失败返回 None。"""
    exe = next((p for p in _BROWSER_EXE_PATHS.get(name, ()) if os.path.exists(p)), None)
    if not exe:
        return None
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), _BROWSER_DIRS[name])
    if not os.path.isdir(root):
        return None
    for port in range(_DEBUG_PORT_BASE, _DEBUG_PORT_MAX):
        try:
            subprocess.Popen(
                [
                    exe,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={root}",
                    "--remote-allow-origins=*",
                    "about:blank",
                ],
                creationflags=_POPEN_DETACHED,
            )
        except Exception:
            continue
        for _ in range(40):
            time.sleep(0.5)
            if _http_json(f"http://127.0.0.1:{port}/json/version") is not None:
                return port
        # 该端口启动失败（可能被占），换下一个端口
    return None


def ensure_debug_browser(force_restart: bool = False) -> tuple[int | None, str]:
    """确保存在可用的调试浏览器实例，返回 (port, detail)。

    - 已有调试实例 → 直接复用；
    - 浏览器在运行但无调试端口：force_restart=True 时结束进程并以调试模式重启；
    - 浏览器未运行 → 直接以调试模式启动。
    """
    port = _find_debug_port()
    if port is not None:
        return port, "已连接正在运行的调试浏览器"
    for name in ("Chrome", "Edge"):
        exe_name = _BROWSER_EXES[name]
        if not os.path.isdir(os.path.join(os.environ.get("LOCALAPPDATA", ""), _BROWSER_DIRS[name])):
            continue
        if _browser_running(exe_name) and not force_restart:
            return None, f"{name} 正在运行但未开启调试端口；需要重启浏览器接入（用户同意后自动处理）"
        if force_restart and _browser_running(exe_name):
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", exe_name],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="replace",
                timeout=10,
                creationflags=_POPEN_DETACHED,
            )
            time.sleep(1.0)
        port = _launch_debug_browser(name)
        if port is not None:
            return port, f"已启动 {name} 调试实例（浏览器保持运行）"
    return None, "未找到 Chrome/Edge 浏览器"


# ══════════ CDP 取 Cookie ══════════


def _extract_bili_cookie(cookies: list[dict]) -> str:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in cookies:
        name = str(c.get("name", ""))
        value = str(c.get("value", ""))
        domain = str(c.get("domain", ""))
        if "bilibili" not in domain:
            continue
        if name not in _KEEP_NAMES or not value or name in seen:
            continue
        if any(ch in value for ch in ';",'):
            continue
        seen.add(name)
        items.append((name, value))
    parts = [f"{n}={v}" for n, v in items]
    return "; ".join(parts)


def get_bili_cookie_via_cdp(port: int) -> tuple[str, str, str]:
    """通过 CDP 从运行中的浏览器读取 B站 Cookie。

    返回 (status, cookie_string, detail)：bound / not_logged_in / fail。
    """
    try:
        ver = _http_json(f"http://127.0.0.1:{port}/json/version", timeout=5)
        if not ver or not ver.get("webSocketDebuggerUrl"):
            return _STATUS_FAIL, "", "调试端口无响应"
        browser_ws = ver["webSocketDebuggerUrl"]
        msg = _cdp_call(browser_ws, "Browser.getCookies", timeout=20)
        if msg.get("error"):
            return _STATUS_FAIL, "", "CDP 调用失败：" + str(msg["error"])
        cookies = msg.get("result", {}).get("cookies", []) or []
        cookie = _extract_bili_cookie(cookies)
        if "SESSDATA=" in cookie:
            return _STATUS_BOUND, cookie, "已从运行中的浏览器读取 B站登录态"
        # 兜底：page 会话的 Network.getAllCookies（个别版本 Browser.getCookies 返回空）
        targets = _http_json(f"http://127.0.0.1:{port}/json/list", timeout=5) or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page and page.get("webSocketDebuggerUrl"):
            msg2 = _cdp_call(page["webSocketDebuggerUrl"], "Network.getAllCookies", timeout=20)
            cookies2 = msg2.get("result", {}).get("cookies", []) or []
            cookie2 = _extract_bili_cookie(cookies2)
            if "SESSDATA=" in cookie2:
                return _STATUS_BOUND, cookie2, "已从运行中的浏览器读取 B站登录态"
        if cookie:
            return _STATUS_NOT_LOGGED_IN, cookie, "浏览器中有游客 Cookie 但无登录态（SESSDATA 缺失或已过期）"
        return _STATUS_NOT_LOGGED_IN, "", "浏览器中未找到 B站 Cookie（登录态缺失或已过期）"
    except Exception as exc:
        return _STATUS_FAIL, "", f"CDP 读取失败：{str(exc)[:80]}"


def bind_via_cdp(force_restart: bool = False) -> tuple[str, str, str]:
    """一键接入正在使用的浏览器：确保调试实例 → CDP 读取 B站 Cookie 并绑定。

    返回 (status, cookie_string, detail)。
    """
    port, detail = ensure_debug_browser(force_restart=force_restart)
    if port is None:
        return _STATUS_NO_BROWSER, "", detail
    return get_bili_cookie_via_cdp(port)
