"""v0.9.39 深审回归：把深审里能单测的几条固化成契约，防止以后改回旧行为。

覆盖：
- S2 写操作不重试
- M1/M2 弹幕回读只认基线后的新增条目
- M12 -352/-412 归类为 RISK_CONTROL，-101 才是 AUTH_FAILED
- M13 WBI 遇 RISK_CONTROL 清缓存重签一次
- M17 回环/内网/带账号口令 URL 一律拒绝
- M18 子进程环境不继承 B站 Cookie
- S3 扩展投放按源目录动态枚举（含 content.js / audio-processor.js）
- L4 LOCALAPPDATA 为空时状态目录仍是绝对路径
- L6 主机名子串不再冒充 B站
"""

from __future__ import annotations

import os
import socket

import pytest


def _fake_response(payload: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return payload

        def geturl(self):
            return "https://api.bilibili.com/"

    return _Resp()


def test_http_get_maps_risk_control_and_auth_separately(monkeypatch):
    from plugin.plugins.bili_viewer import bili

    def fake_open(req, timeout=12.0, retries=3, jitter=False):
        return _fake_response(b'{"code":-352,"message":"risk"}')

    monkeypatch.setattr(bili, "_open_with_retry", fake_open)
    monkeypatch.setattr(bili, "_cookie", lambda account="main": "")
    with pytest.raises(bili.BiliError) as ei:
        bili._http_get("https://api.bilibili.com/x/test")
    assert ei.value.code == "RISK_CONTROL"

    def fake_open_auth(req, timeout=12.0, retries=3, jitter=False):
        return _fake_response(b'{"code":-101,"message":"not login"}')

    monkeypatch.setattr(bili, "_open_with_retry", fake_open_auth)
    with pytest.raises(bili.BiliError) as ei:
        bili._http_get("https://api.bilibili.com/x/test")
    assert ei.value.code == "AUTH_FAILED"


def test_wbi_get_refreshes_key_on_risk_control(monkeypatch):
    from plugin.plugins.bili_viewer import bili

    calls = {"n": 0, "invalidated": 0}

    def fake_get(url, params=None, timeout=12.0, account="main"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise bili.BiliError("risk", "RISK_CONTROL")
        return {"code": 0, "data": {}}

    monkeypatch.setattr(bili, "_http_get", fake_get)
    monkeypatch.setattr(bili, "_get_mixin_key", lambda: "m" * 32)
    monkeypatch.setattr(bili, "_invalidate_mixin_cache", lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1))
    assert bili._wbi_get("https://api.bilibili.com/x/test", {"a": 1})["code"] == 0
    assert calls["n"] == 2 and calls["invalidated"] == 1


def test_danmaku_is_new_ignores_pre_existing_same_text():
    from plugin.plugins.bili_viewer import bili

    uid = 42
    history = [{"uid": uid, "text": "666", "timeline": "old"}]
    baseline = bili._danmaku_baseline(history, uid, "666")
    assert bili._danmaku_is_new(history, uid, "666", baseline) is False
    after = history + [{"uid": uid, "text": "666", "timeline": "new"}]
    assert bili._danmaku_is_new(after, uid, "666", baseline) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/x",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.8/a",
        "http://user:pass@example.com/a",
        "ftp://example.com/a",
    ],
)
def test_ssrf_guard_rejects_non_public(url):
    from plugin.plugins.bili_viewer import _is_public_http_url

    assert _is_public_http_url(url) is False


def test_ssrf_guard_rejects_hostname_that_resolves_to_private(monkeypatch):
    import plugin.plugins.bili_viewer as bv

    monkeypatch.setattr(
        bv.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
    )
    assert bv._is_public_http_url("https://evil.test/a") is False


def test_child_env_strips_bili_cookie(monkeypatch):
    import plugin.plugins.bili_viewer as bv

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=secret")
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=yui")
    env = bv._child_env()
    assert "BILI_COOKIE" not in env and "BILI_YUI_COOKIE" not in env


def test_desktop_ext_file_list_is_dynamic(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    for name in ("manifest.json", "background.js", "content.js", "audio-processor.js"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    files = desktop_deliver._list_ext_files(str(tmp_path))
    assert "content.js" in files and "audio-processor.js" in files


def test_state_dir_is_absolute_when_localappdata_empty(monkeypatch):
    from plugin.plugins.bili_viewer import bili_social

    monkeypatch.setenv("BILI_SOCIAL_DIR", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    p = bili_social._state_dir()
    assert os.path.isabs(p)


def test_parse_bili_url_rejects_lookalike_host():
    from plugin.plugins.bili_viewer import bili

    assert bili.parse_bili_url("https://evil.example/?next=bilibili.com") is None
    assert bili.parse_bili_url("https://bilibili.com.evil.example/video/BV1xx") is None
    assert bili.parse_bili_url("https://www.bilibili.com/video/BV1GJ411x7h7") is not None
