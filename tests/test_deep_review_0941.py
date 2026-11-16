"""v0.9.41 增量深审回归：全部离线，不访问真实 B站或浏览器。"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest


def _bili():
    from plugin.plugins.bili_viewer import bili

    return bili


def _pkg():
    import plugin.plugins.bili_viewer as pkg

    return pkg


def _inst():
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    return BiliViewerPlugin(ctx=None)


def _web():
    from plugin.plugins.bili_viewer import web_ui

    return web_ui


def _panel():
    web_ui = _web()
    panel = web_ui.BindPanel(lambda: {}, lambda: {"ok": True}, ext_fn=lambda *_: {"ok": True}, port=0)
    url, started = panel.ensure_started()
    assert started is True
    return panel, url


def _json_post(url: str, body: dict, origin: str = "") -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ── 写请求不重试，连接中断必须进入“结果未知”语义 ──────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        http.client.RemoteDisconnected("closed"),
        http.client.BadStatusLine("bad"),
        ConnectionResetError(10054, "reset"),
        OSError(10053, "aborted"),
    ],
)
def test_post_transport_interruption_is_ambiguous_and_not_retried(monkeypatch, exc):
    bili = _bili()
    calls = []

    def fake_open(req, timeout):
        calls.append(req)
        raise exc

    monkeypatch.setattr(bili.urllib.request, "urlopen", fake_open)
    req = urllib.request.Request("https://example.invalid/write", data=b"x=1")
    with pytest.raises(bili.BiliError) as caught:
        bili._open_with_retry(req, 1.0, retries=0)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert bili.write_outcome_ambiguous(caught.value)
    assert "人工核实" in caught.value.message
    assert len(calls) == 1


def test_get_transport_interruption_retries_then_succeeds(monkeypatch):
    bili = _bili()
    calls = []
    sentinel = object()

    def fake_open(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise http.client.RemoteDisconnected("closed")
        return sentinel

    monkeypatch.setattr(bili.urllib.request, "urlopen", fake_open)
    req = urllib.request.Request("https://example.invalid/read")
    assert bili._open_with_retry(req, 1.0, retries=2) is sentinel
    assert len(calls) == 3


def test_like_ambiguous_error_keeps_rate_reservation(monkeypatch):
    from plugin.plugins.bili_viewer import bili_social

    bili = _bili()
    monkeypatch.setattr(bili, "_require_login", lambda account: "fake")
    monkeypatch.setattr(bili, "_csrf", lambda account: "csrf")
    monkeypatch.setattr(bili_social, "_bvid_aid", lambda bvid, account: (bvid, 123))
    monkeypatch.setattr(bili_social, "check_limit", lambda *a, **k: 1.0)

    def fail_post(*args, **kwargs):
        raise bili.BiliError("连接中断", "WRITE_OUTCOME_UNKNOWN")

    def forbidden_release(*args, **kwargs):
        raise AssertionError("歧义写错误不得释放限流预占")

    monkeypatch.setattr(bili, "_http_post", fail_post)
    monkeypatch.setattr(bili_social, "release_limit", forbidden_release)
    with pytest.raises(bili.BiliError) as caught:
        bili_social.video_like("BV1GJ411x7h7")
    assert caught.value.code == "LIKE_MAYBE_SENT"
    assert "已保留" in caught.value.message


# ── 评论显式目标与歧义文案 ────────────────────────────────────────────────


def test_explicit_video_target_failure_never_falls_back_to_current_tab(monkeypatch):
    bili = _bili()
    inst = _inst()
    calls = {"tab": 0, "reply": 0}

    def fail_info(*args, **kwargs):
        raise bili.BiliError("offline", "NETWORK")

    monkeypatch.setattr(bili, "video_info", fail_info)
    monkeypatch.setattr(inst, "_ext_tab", lambda **kwargs: calls.__setitem__("tab", calls["tab"] + 1) or {
        "url": "https://live.bilibili.com/99999", "title": "别的直播间"
    })
    monkeypatch.setattr(bili, "add_reply", lambda *a, **k: calls.__setitem__("reply", calls["reply"] + 1))
    result = asyncio.run(inst.bili_reply_comment(target="BV1GJ411x7h7", text="test", account="main"))
    assert result["error"] == "NO_TARGET"
    assert calls == {"tab": 0, "reply": 0}


def test_missing_target_can_still_use_current_live_tab(monkeypatch):
    bili = _bili()
    inst = _inst()
    replies = []
    monkeypatch.setattr(inst, "_ext_tab", lambda **kwargs: {
        "url": "https://live.bilibili.com/99999", "title": "当前直播间"
    })
    monkeypatch.setattr(bili, "add_reply", lambda oid, kind, text, account: replies.append((oid, kind, text, account)) or {})
    result = asyncio.run(inst.bili_reply_comment(text="test", account="main"))
    assert result["is_error"] is False
    assert replies == [(99999, 11, "test", "main")]


def test_main_comment_ambiguity_does_not_claim_nonexistent_rate_window(monkeypatch):
    bili = _bili()
    inst = _inst()

    def fail_reply(*args, **kwargs):
        raise bili.BiliError("连接中断", "WRITE_OUTCOME_UNKNOWN")

    monkeypatch.setattr(bili, "add_reply", fail_reply)
    result = asyncio.run(inst.bili_reply_comment(target="99999", text="test", account="main"))
    assert result["error"] == "COMMENT_MAYBE_SENT"
    assert "人工核实" in result["output"]
    assert "已保留" not in result["output"]


# ── 独立 Cookie 验真器：缓存、短重试、单飞与锁边界 ─────────────────────────


def test_cookie_verifier_transient_negative_cache_expires_and_recovers():
    from plugin.plugins.bili_viewer import bili, cookie_validation

    now = [1000.0]
    calls = []
    sleeps = []

    def validator(cookie, timeout):
        calls.append(cookie)
        if len(calls) <= 2:
            raise bili.BiliError("risk", "RISK_CONTROL")
        return 42

    verifier = cookie_validation.CookieVerifier(
        validator, clock=lambda: now[0], sleeper=sleeps.append, retry_delay=0.01
    )
    first = verifier.verify("fake-cookie")
    assert first.status == cookie_validation.UNAVAILABLE
    assert first.code == "RISK_CONTROL"
    assert len(calls) == 2 and sleeps == [0.01]
    cached = verifier.verify("fake-cookie")
    assert cached.from_cache is True and len(calls) == 2
    now[0] += 31
    recovered = verifier.verify("fake-cookie")
    assert recovered.status == cookie_validation.VERIFIED and recovered.uid == 42
    assert len(calls) == 3
    assert verifier.verify("fake-cookie").from_cache is True
    assert len(calls) == 3


@pytest.mark.parametrize("code", ["BAD_COOKIE", "AUTH_FAILED", "NEED_LOGIN"])
def test_cookie_verifier_permanent_rejection_has_no_retry(code):
    from plugin.plugins.bili_viewer import bili, cookie_validation

    calls = []

    def reject(cookie, timeout):
        calls.append(cookie)
        raise bili.BiliError("rejected", code)

    verifier = cookie_validation.CookieVerifier(reject, sleeper=lambda _: pytest.fail("不应重试"))
    assert verifier.verify("fake").status == cookie_validation.REJECTED
    assert verifier.verify("fake").from_cache is True
    assert len(calls) == 1


def test_cookie_verifier_same_cookie_singleflight():
    from plugin.plugins.bili_viewer import cookie_validation

    calls = []
    entered = threading.Event()
    release = threading.Event()
    call_lock = threading.Lock()

    def validator(cookie, timeout):
        with call_lock:
            calls.append(cookie)
        entered.set()
        assert release.wait(5)
        return 42

    verifier = cookie_validation.CookieVerifier(validator, retry_delay=0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(verifier.verify, "same-fake-cookie") for _ in range(8)]
        assert entered.wait(5)
        time.sleep(0.05)
        release.set()
        results = [future.result(5) for future in futures]
    assert len(calls) == 1
    assert all(r.status == cookie_validation.VERIFIED and r.uid == 42 for r in results)


def test_assign_cookie_validates_outside_global_account_lock(monkeypatch):
    bili = _bili()
    pkg = _pkg()
    inst = _inst()
    state = {"held": False, "entered": False}

    class TrackingLock:
        def __enter__(self):
            state["held"] = True
            state["entered"] = True
            return self

        def __exit__(self, *args):
            state["held"] = False

    def validator(cookie, timeout=6.0):
        assert state["held"] is False
        return 123

    monkeypatch.setattr(pkg, "_ACCOUNTS_LOCK", TrackingLock())
    monkeypatch.setattr(bili, "validate_cookie", validator)
    monkeypatch.setattr(inst, "_assign_cookie_locked", lambda cookie, at, source, uid: {
        "ok": True, "bound": True, "uid": uid, "lock_held": state["held"]
    })
    result = inst._assign_cookie("SESSDATA=fake; DedeUserID=123", 0, "test")
    assert result["bound"] is True and result["uid"] == 123
    assert result["lock_held"] is True and state["entered"] is True


# ── 状态路径、权威角色与并发 Cookie 刷新 ──────────────────────────────────


def test_loop_path_social_override_and_home_fallback(monkeypatch, tmp_path):
    from plugin.plugins.bili_viewer import bili_social

    inst = _inst()
    override = tmp_path / "social"
    monkeypatch.setenv("BILI_SOCIAL_DIR", str(override))
    assert inst._loop_path() == str(override / "danmaku_loop.json")
    monkeypatch.delenv("BILI_SOCIAL_DIR")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(bili_social.os.path, "expanduser", lambda value: str(home) if value == "~" else value)
    expected = home / "N.E.K.O" / "bili_viewer" / "bili_social" / "danmaku_loop.json"
    assert inst._loop_path() == str(expected)
    assert not str(expected).startswith("N.E.K.O")


def test_cookie_refresh_accounts_authoritative_and_concurrent_atomic(monkeypatch, tmp_path):
    bili = _bili()
    web_ui = _web()
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(web_ui, "_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "_encode_cookie_file", lambda value: bili._COOKIE_FILE_PLAIN_MAGIC + value)
    (tmp_path / "accounts.json").write_text(json.dumps({
        "main": {"uid": 111}, "yui": {"uid": 222}
    }), encoding="utf-8")
    # 旧 role 故意与权威 accounts 冲突，不能据此串号。
    (tmp_path / "role.json").write_text(json.dumps({"111": "yui", "222": "main"}), encoding="utf-8")

    def refresh(uid, index):
        cookie = f"SESSDATA={uid}-{index}; bili_jct=jct; DedeUserID={uid}"
        return web_ui._refresh_ext_cookie(cookie)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(refresh, uid, i) for i in range(20) for uid in (111, 222)]
        results = [future.result(5) for future in futures]
    assert all(item["updated"] for item in results)
    assert {item["file"] for item in results if item["uid"] == "111"} == {"main_cookie.txt"}
    assert {item["file"] for item in results if item["uid"] == "222"} == {"yui_cookie.txt"}
    main = bili._read_cookie_file(str(tmp_path / "main_cookie.txt"))
    yui = bili._read_cookie_file(str(tmp_path / "yui_cookie.txt"))
    assert "DedeUserID=111" in main and "DedeUserID=222" not in main
    assert "DedeUserID=222" in yui and "DedeUserID=111" not in yui
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


# ── 本机 HTTP 面板：非 ASCII 鉴权、弹幕日志、超限请求体 ─────────────────────


def test_secure_equal_supports_non_ascii_without_type_error():
    web_ui = _web()
    assert web_ui._secure_equal("令牌", "令牌") is True
    assert web_ui._secure_equal("令牌", "token") is False
    assert web_ui._secure_equal("token", "令牌") is False


def test_panel_percent_encoded_non_ascii_bootstrap_is_clean_403():
    panel, url = _panel()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url + "?k=" + urllib.parse.quote("令牌"), timeout=5)
        assert caught.value.code == 403
    finally:
        panel.stop()


def test_panel_raw_non_ascii_token_header_is_clean_403():
    panel, url = _panel()
    try:
        port = urllib.parse.urlsplit(url).port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            request = (
                f"POST /api/bind HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
                "X-Neko-Token: ".encode() + "令牌".encode("utf-8") +
                b"\r\nContent-Length: 0\r\n\r\n"
            )
            sock.sendall(request)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        assert response.startswith(b"HTTP/1.0 403") or response.startswith(b"HTTP/1.1 403")
    finally:
        panel.stop()


def test_danmaku_buffer_keeps_content_but_info_log_redacts_it(caplog):
    web_ui = _web()
    panel, url = _panel()
    sender = "unique-sender-0941"
    text = "unique-danmaku-text-0941"
    caplog.set_level(logging.INFO, logger=web_ui._logger.name)
    try:
        result = _json_post(url + "api/extension-danmaku", {
            "sender": sender, "text": text, "room": "12345", "at": 1
        }, origin=web_ui._EXTENSION_ORIGIN)
        assert result["ok"] is True
        assert web_ui._DANMAKU_BUF[-1][:2] == (sender, text)
        records = [record for record in caplog.records if "ext danmaku:" in record.getMessage()]
        assert records and "text_len=" in records[-1].getMessage()
        logged = "\n".join(record.getMessage() for record in records)
        assert sender not in logged and text not in logged
    finally:
        panel.stop()


def test_oversized_cookie_body_gets_response_not_connection_reset():
    web_ui = _web()
    received = []
    panel = web_ui.BindPanel(
        lambda: {}, lambda: {},
        ext_fn=lambda cookie, at, source="": received.append(cookie) or {"ok": True}, port=0,
    )
    url, _ = panel.ensure_started()
    try:
        result = _json_post(url + "api/extension-cookie", {
            "cookie": "SESSDATA=" + "x" * 100_000, "at": 1
        }, origin=web_ui._EXTENSION_ORIGIN)
        assert result["ok"] is True
        assert received == [""]
    finally:
        panel.stop()


# ── CDP 仅允许真实 loopback WebSocket ──────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "ws://evil.example/devtools/browser/1",
        "ws://127.0.0.1.evil.example/devtools/browser/1",
        "ws://127.0.0.1@evil.example/devtools/browser/1",
        "ws://2130706433/devtools/browser/1",
        "ws://[::ffff:127.0.0.1]/devtools/browser/1",
        "http://127.0.0.1/devtools/browser/1",
    ],
)
def test_cdp_rejects_external_or_spoofed_websocket_without_connect(monkeypatch, url):
    from plugin.plugins.bili_viewer import cdp_cookie

    def forbidden(*args, **kwargs):
        raise AssertionError("恶意 WebSocket 不得触发 socket 连接")

    monkeypatch.setattr(cdp_cookie.socket, "create_connection", forbidden)
    with pytest.raises(OSError):
        cdp_cookie._ws_connect(url)


def test_cdp_localhost_dns_rebinding_is_rejected(monkeypatch):
    from plugin.plugins.bili_viewer import cdp_cookie

    monkeypatch.setattr(cdp_cookie.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 9222))
    ])
    with pytest.raises(OSError, match="loopback"):
        cdp_cookie._loopback_ws_target("ws://localhost:9222/devtools/browser/1")


def test_cdp_ipv6_loopback_and_external_debugger_url(monkeypatch):
    from plugin.plugins.bili_viewer import cdp_cookie

    assert cdp_cookie._loopback_ws_target("ws://[::1]:9222/devtools/browser/1?x=1") == (
        "::1", 9222, "/devtools/browser/1?x=1"
    )
    monkeypatch.setattr(cdp_cookie, "_http_json", lambda *a, **k: {
        "webSocketDebuggerUrl": "ws://evil.example/devtools/browser/1"
    })
    monkeypatch.setattr(
        cdp_cookie.socket, "create_connection",
        lambda *a, **k: pytest.fail("恶意 debugger URL 不得触发 socket 连接"),
    )
    status, cookie, detail = cdp_cookie.get_bili_cookie_via_cdp(9222)
    assert status == cdp_cookie._STATUS_FAIL and cookie == ""
    assert "CDP 读取失败" in detail


# ── POST helper 只读取一次 Cookie ──────────────────────────────────────────


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"code": 0, "data": {}}'


@pytest.mark.parametrize("kind", ["form", "json", "multipart"])
def test_post_helpers_resolve_cookie_once(monkeypatch, kind):
    bili = _bili()
    calls = []

    def cookie(account):
        calls.append(account)
        return "SESSDATA=fake"

    monkeypatch.setattr(bili, "_cookie", cookie)
    monkeypatch.setattr(bili, "_open_with_retry", lambda *a, **k: _Response())
    if kind == "form":
        result = bili._http_post("https://example.invalid", {"x": 1})
    elif kind == "json":
        result = bili._http_post_json("https://example.invalid", {"x": 1}, "csrf")
    else:
        result = bili._http_multipart("https://example.invalid", {"x": 1}, {"file": ("a.bin", b"x")})
    assert result["code"] == 0
    assert len(calls) == 1


# ── 限流状态损坏/不可写时 fail-closed ──────────────────────────────────────


def test_ratelimit_missing_file_works_but_corrupt_json_blocks(monkeypatch, tmp_path):
    from plugin.plugins.bili_viewer import bili_social

    monkeypatch.setenv("BILI_SOCIAL_DIR", str(tmp_path))
    bili_social._RL_PENDING.clear()
    assert bili_social.check_limit("video_like") is not None
    bili_social._RL_PENDING.clear()
    (tmp_path / "ratelimit.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(_bili().BiliError) as caught:
        bili_social.check_limit("coin")
    assert caught.value.code == "RATE_STATE_UNAVAILABLE"


def test_ratelimit_write_failure_blocks_before_network(monkeypatch, tmp_path):
    from plugin.plugins.bili_viewer import bili_social

    monkeypatch.setenv("BILI_SOCIAL_DIR", str(tmp_path))
    bili_social._RL_PENDING.clear()

    def fail_replace(*args):
        raise OSError("simulated read-only state directory")

    monkeypatch.setattr(bili_social.os, "replace", fail_replace)
    with pytest.raises(_bili().BiliError) as caught:
        bili_social.check_limit("dm")
    assert caught.value.code == "RATE_STATE_UNAVAILABLE"
    assert bili_social._RL_PENDING == {}
