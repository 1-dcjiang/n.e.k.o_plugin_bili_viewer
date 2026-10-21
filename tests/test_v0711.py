"""v0.7.11/0.7.12 新增能力测试：声音通道（_receive_ext_audio / bili_listen_audio）、
网络层自动重试（_open_with_retry）、AUTH_FAILED 自愈（_wrap_auth + 60s 冷却）、
三扩展包标识对称（manifest 0.2.6 + EXT_SOURCE + 新增 offscreen 文件）。

注意：测试全程不触网（urlopen 由 monkeypatch 接管；ASR 模型懒加载不触发）。
"""

from __future__ import annotations

import base64
import json
import os
import time

import pytest


def _wav_base64(seconds: float = 1.0, rate: int = 16000) -> str:
    """生成一个 16kHz/16bit/mono 的静音 wav（带短促音）的 base64 dataURL。"""
    n = int(seconds * rate)
    data_len = n * 2
    buf = bytearray(44 + data_len)
    buf[0:4] = b"RIFF"
    buf[8:12] = b"WAVE"
    buf[12:16] = b"fmt "
    buf[36:40] = b"data"
    import struct as _s
    _s.pack_into("<IHHIIHH", buf, 16, 16, 1, 1, rate, rate * 2, 2, 16)
    _s.pack_into("<I", buf, 4, 36 + data_len)
    _s.pack_into("<I", buf, 40, data_len)
    for i in range(n):
        _s.pack_into("<h", buf, 44 + i * 2, 0)  # 静音
    return "data:audio/wav;base64," + base64.b64encode(bytes(buf)).decode()


def _pkg():
    # 双模块教训（v0.7.10）：patch 包模块而非 importlib 副本，才能影响类方法全局
    import plugin.plugins.bili_viewer as pkg

    return pkg


def _make_instance(pkg) -> object:
    return pkg.BiliViewerPlugin.__new__(pkg.BiliViewerPlugin)


# ══════════════ 扩展声音接收 ══════════════


def test_receive_ext_audio_valid_saves_and_kicks_asr(tmp_path, monkeypatch):
    pkg = _pkg()
    inst = _make_instance(pkg)
    monkeypatch.setattr(pkg, "_AUDIO_PATH", os.path.join(str(tmp_path), "seg.wav"))
    kicked = []

    def fake_kick():
        kicked.append(True)

    monkeypatch.setattr(inst, "_kick_asr", fake_kick)
    body = {"audio": _wav_base64(1.0), "sample_rate": 16000, "seq": 7, "at": 1234567890}
    res = inst._receive_ext_audio(body)
    assert res.get("ok") is True
    assert os.path.isfile(pkg._AUDIO_PATH)
    seg = inst._ext_audio()
    assert seg is not None and seg["seq"] == 7
    assert kicked == [True]


def test_receive_ext_audio_rejects_bad_payloads(tmp_path, monkeypatch):
    pkg = _pkg()
    inst = _make_instance(pkg)
    monkeypatch.setattr(pkg, "_AUDIO_PATH", os.path.join(str(tmp_path), "seg.wav"))
    assert inst._receive_ext_audio({}).get("ok") is False
    assert inst._receive_ext_audio({"audio": "not-a-dataurl"}).get("ok") is False
    # 超体积拒绝（构造 5MB base64 静音）
    big = "data:audio/wav;base64," + base64.b64encode(b"\x00" * (5 * 1024 * 1024)).decode()
    assert inst._receive_ext_audio({"audio": big}).get("ok") is False
    assert not os.path.exists(pkg._AUDIO_PATH)


# ══════════════ bili_listen_audio 工具 ══════════════


def test_listen_audio_returns_fresh_asr_text():
    pkg = _pkg()
    inst = _make_instance(pkg)
    inst._ext_asr_data = {"text": "主播说今天比赛很精彩", "lang": "zh", "at": 9999999999.0, "seq": 3}
    import asyncio

    res = asyncio.run(inst.bili_listen_audio())
    assert res["is_error"] is False
    assert "今天比赛很精彩" in res["output"]


def test_listen_audio_empty_when_stale():
    pkg = _pkg()
    inst = _make_instance(pkg)
    inst._ext_asr_data = {"text": "旧内容", "lang": "zh", "at": 1.0, "seq": 3}  # 已过期
    import asyncio

    res = asyncio.run(inst.bili_listen_audio())
    assert res["is_error"] is True
    assert res["error"] == "NO_AUDIO"


# ══════════════ 网络层自动重试（_open_with_retry） ══════════════


def test_http_get_retries_transient_network_error(monkeypatch):
    import plugin.plugins.bili_viewer.bili as bili

    calls = {"n": 0}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"code":0,"data":{"ok":1}}'

    def fake_urlopen(req, timeout=12.0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("boom")
        return _FakeResp()

    import urllib.error

    monkeypatch.setattr(bili.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bili, "_cookie", lambda account="main": "")
    data = bili._http_get("https://api.bilibili.com/x/test", {"a": 1})
    assert data["code"] == 0
    assert calls["n"] == 3  # 2 次失败重试 + 1 次成功


def test_http_post_jitter_retries_and_passes_business_errors(monkeypatch):
    import plugin.plugins.bili_viewer.bili as bili

    calls = {"n": 0}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"code":0,"data":{}}'

    def fake_urlopen(req, timeout=12.0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("boom")
        return _FakeResp()

    import urllib.error

    monkeypatch.setattr(bili.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bili, "_cookie", lambda account="main": "")
    data = bili._http_post("https://api.bilibili.com/x/post", {"a": 1})
    assert data["code"] == 0
    assert calls["n"] == 3

    # 业务错误不重试
    calls["n"] = 0

    def fake_biz(req, timeout=12.0):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)

    monkeypatch.setattr(bili.urllib.request, "urlopen", fake_biz)
    with pytest.raises(bili.BiliError) as ei:
        bili._http_post("https://api.bilibili.com/x/post", {"a": 1})
    assert ei.value.code == "HTTP_ERROR"
    assert calls["n"] == 1  # 4xx 不重试


# ══════════════ AUTH_FAILED 自愈 ══════════════


def test_wrap_auth_triggers_rebind_on_auth_failed():
    pkg = _pkg()
    inst = _make_instance(pkg)
    rebound = []

    def fake_bg_bind():
        rebound.append(True)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inst, "_bg_bind", fake_bg_bind)
    exc = pkg.bili.BiliError("登录态失效", "AUTH_FAILED")
    res = inst._wrap_auth(exc, "发送弹幕")
    monkeypatch.undo()
    assert res["is_error"] is True
    assert res["error"] == "AUTH_FAILED"
    assert "自动尝试重新接入" in res["output"]
    assert rebound == [True]


def test_wrap_auth_passthrough_other_errors():
    pkg = _pkg()
    inst = _make_instance(pkg)
    exc = pkg.bili.BiliError("风控", "RISK_CONTROL")
    res = inst._wrap_auth(exc, "发送弹幕")
    assert res["is_error"] is True
    assert res["error"] == "RISK_CONTROL"
    assert "自动尝试" not in res["output"]


def test_wrap_auth_rebind_cooldown_60s():
    """v0.7.12：同实例 60s 内重复 AUTH_FAILED 不反复触发后台重绑。"""
    pkg = _pkg()
    inst = _make_instance(pkg)
    rebound = []

    def fake_bg_bind():
        rebound.append(True)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inst, "_bg_bind", fake_bg_bind)
    exc = pkg.bili.BiliError("登录态失效", "AUTH_FAILED")
    inst._auth_rebind_at = 0.0
    inst._wrap_auth(exc, "发送弹幕")   # 首次：触发
    assert rebound == [True]
    inst._wrap_auth(exc, "发送弹幕")   # 冷却内：不再触发
    assert rebound == [True]
    inst._auth_rebind_at = time.time() - 61.0
    inst._wrap_auth(exc, "发送弹幕")   # 冷却过期：再次触发
    assert rebound == [True, True]
    monkeypatch.undo()


# ══════════════ 三扩展包标识对称（真实文件字节级） ══════════════

_EXT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser_extension"
)


def _read_ext(pkg_name: str, fname: str) -> str:
    with open(os.path.join(_EXT_ROOT, pkg_name, fname), "r", encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("pkg_name", ["bili-bridge-chrome", "bili-bridge-edge", "bili-bridge-360"])
def test_ext_package_has_all_six_files(pkg_name):
    for f in ("manifest.json", "background.js", "popup.html", "popup.js", "offscreen.html", "offscreen.js"):
        assert os.path.isfile(os.path.join(_EXT_ROOT, pkg_name, f)), f"{pkg_name}/{f} 缺失"


@pytest.mark.parametrize("pkg_name", ["bili-bridge-chrome", "bili-bridge-edge", "bili-bridge-360"])
def test_ext_manifest_has_audio_permissions(pkg_name):
    manifest = json.loads(_read_ext(pkg_name, "manifest.json"))
    assert manifest["version"] == "0.2.6"
    assert "tabCapture" in manifest["permissions"]
    assert "offscreen" in manifest["permissions"]


@pytest.mark.parametrize(
    ("pkg_name", "source"),
    [
        ("bili-bridge-chrome", "bili-bridge-chrome-extension"),
        ("bili-bridge-edge", "bili-bridge-edge-extension"),
        ("bili-bridge-360", "bili-bridge-360-extension"),
    ],
)
def test_ext_extension_sources_distinct(pkg_name, source):
    bg = _read_ext(pkg_name, "background.js")
    assert f'const EXT_SOURCE = "{source}";' in bg
    # 同包内不得出现其他浏览器的 source
    for other in ("chrome-extension", "edge-extension", "360-extension"):
        if other != source:
            assert f'EXT_SOURCE = "{other}"' not in bg


def test_ext_offscreen_pushes_to_panel_audio_endpoint():
    js = _read_ext("bili-bridge-chrome", "offscreen.js")
    assert "/api/extension-audio" in js
    assert "127.0.0.1" in js
    html = _read_ext("bili-bridge-chrome", "offscreen.html")
    assert "offscreen.js" in html


def test_ext_background_has_audio_state_and_recovery():
    bg = _read_ext("bili-bridge-chrome", "background.js")
    assert "audio_capture_enabled" in bg
    assert "start-audio" in bg
    assert "stop-audio" in bg
    assert "ensureAudioSession" in bg
