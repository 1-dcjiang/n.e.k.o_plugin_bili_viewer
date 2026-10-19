"""web_ui 面板端点测试：页面 / 一键接入 / 扩展推送 / 扩展状态。

使用随机端口启动真实本地 HTTP 服务，验证扩展通道（bili-bridge）可直连插件。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request


def _web_ui():
    from plugin.plugins.bili_viewer import web_ui

    return web_ui


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _start_panel(ext_fn):
    web_ui = _web_ui()

    def status_fn():
        return {
            "ok": True,
            "browser": "已连接调试浏览器",
            "browser_ok": True,
            "bound": False,
            "cookie_manual": False,
            "cookie_source": "none",
            "extension_online": False,
            "extension_last_seen": 0,
            "uname": "",
        }

    def bind_fn():
        return {"ok": True, "bound": True, "detail": "已启动调试实例"}

    panel = web_ui.BindPanel(status_fn, bind_fn, ext_fn=ext_fn, port=_free_port())
    url, started = panel.ensure_started()
    assert started is True
    return panel, url


def test_panel_serves_page(monkeypatch, tmp_path):
    panel, url = _start_panel(lambda *_: {"ok": True})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        assert "一键接入" in html
        assert "B站接入面板" in html
    finally:
        panel._server.shutdown()


def test_panel_bind_endpoint(monkeypatch, tmp_path):
    panel, url = _start_panel(lambda *_: {"ok": True})
    try:
        result = _http_json(url + "api/bind", method="POST")
        assert result["ok"] is True
        assert result["bound"] is True
    finally:
        panel._server.shutdown()


def test_extension_cookie_endpoint(monkeypatch, tmp_path):
    received = []
    panel, url = _start_panel(lambda cookie, at: (received.append((cookie, at)) or {"ok": True, "bound": True}))
    try:
        body = {"cookie": "SESSDATA=abc; bili_jct=xyz", "at": int(time.time() * 1000), "source": "bili-bridge-extension"}
        result = _http_json(url + "api/extension-cookie", method="POST", body=body)
        assert result["ok"] is True
        assert received and received[0][0] == "SESSDATA=abc; bili_jct=xyz"
        assert received[0][1] == body["at"]
    finally:
        panel._server.shutdown()


def test_extension_status_endpoint(monkeypatch, tmp_path):
    panel, url = _start_panel(lambda *_: {"ok": True})
    try:
        result = _http_json(url + "api/extension-status")
        assert "online" in result
        assert "last_seen" in result
    finally:
        panel._server.shutdown()


def test_panel_started_once(monkeypatch, tmp_path):
    panel, url = _start_panel(lambda *_: {"ok": True})
    try:
        url2, started2 = panel.ensure_started()
        assert started2 is False
        assert url2 == url
    finally:
        panel._server.shutdown()


def test_panel_stop_releases_port(monkeypatch, tmp_path):
    """stop() 释放端口后，同一端口可再次启动（插件重载场景）。"""
    web_ui = _web_ui()
    panel, url = _start_panel(lambda *_: {"ok": True})
    port = panel._port
    panel.stop()
    # 停止后端口立即可复用（ThreadingHTTPServer 默认 SO_REUSEADDR）
    panel2 = web_ui.BindPanel(
        panel._status_fn, panel._bind_fn, ext_fn=panel._ext_fn, port=port
    )
    url2, started2 = panel2.ensure_started()
    try:
        assert started2 is True
        assert url2 == f"http://127.0.0.1:{port}/"
    finally:
        panel2.stop()
