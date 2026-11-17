"""v0.9.43 增量深审回归：全部离线，不访问真实 B站、浏览器或网络。"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request

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


def _json_post(url: str, body: dict, origin: str = "") -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _panel(**kwargs):
    web_ui = _web()
    kwargs.setdefault("status_fn", lambda: {})
    kwargs.setdefault("bind_fn", lambda: {})
    panel = web_ui.BindPanel(
        kwargs.pop("status_fn"),
        kwargs.pop("bind_fn"),
        port=0,
        **kwargs,
    )
    url, started = panel.ensure_started()
    assert started is True
    return panel, url


# ── 评论失败日志：error 与 target 不能再串位 ─────────────────────────────


def test_comment_failure_log_keeps_error_and_target_separate(monkeypatch, caplog):
    bili = _bili()
    inst = _inst()

    def fail(*args, **kwargs):
        raise bili.BiliError("普通业务失败", "BIZ_500")

    monkeypatch.setattr(bili, "add_reply", fail)
    caplog.set_level(logging.INFO)
    result = asyncio.run(inst.bili_reply_comment(target="12345", text="hi", account="main"))
    assert result["is_error"] is True
    records = [r.getMessage() for r in caplog.records if "comment send result" in r.getMessage()]
    assert records
    assert "error=BIZ_500" in records[-1]
    assert "target=11:12345" in records[-1]


# ── 扩展推送日志：URL/标题不能落 INFO/DEBUG 文本 ──────────────────────────


def test_extension_tab_dom_shot_logs_do_not_contain_url_or_title(caplog):
    web_ui = _web()
    secret_url = "https://www.bilibili.com/video/BV1SECRETPATH?p=9"
    secret_title = "私人浏览标题"
    panel, url = _panel(
        ext_fn=lambda *_: {"ok": True},
        tab_fn=lambda *_: {"ok": True, "is_bili": True},
        dom_fn=lambda body: {"ok": True, "is_bili": True},
        shot_fn=lambda body: {"ok": True, "is_bili": True},
    )
    caplog.set_level(logging.DEBUG, logger=web_ui._logger.name)
    try:
        assert _json_post(url + "api/extension-tab", {
            "url": secret_url, "title": secret_title, "at": 1, "source": "ext",
        }, origin=web_ui._EXTENSION_ORIGIN)["ok"] is True
        assert _json_post(url + "api/extension-dom", {
            "url": secret_url, "title": secret_title, "kind": "video", "html": "<p>x</p>",
        }, origin=web_ui._EXTENSION_ORIGIN)["ok"] is True
        assert _json_post(url + "api/extension-shot", {
            "url": secret_url, "title": secret_title, "image": "A" * 80,
        }, origin=web_ui._EXTENSION_ORIGIN)["ok"] is True
    finally:
        panel.stop()
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_url not in messages
    assert secret_title not in messages
    assert "ext tab:" in messages and "url_len=" in messages
    assert "ext dom:" in messages and "title_len=" in messages
    assert "ext shot:" in messages and "img_len=" in messages


# ── 扩展 Cookie 端点：归位已写盘，不能再二次 _refresh_ext_cookie ──────────


def test_extension_cookie_endpoint_does_not_double_write(monkeypatch):
    web_ui = _web()

    def forbidden(_cookie):
        raise AssertionError("不应再调用 _refresh_ext_cookie")

    monkeypatch.setattr(web_ui, "_refresh_ext_cookie", forbidden)
    panel, url = _panel(
        ext_fn=lambda cookie, at, source="": {"ok": True, "bound": True},
    )
    try:
        result = _json_post(url + "api/extension-cookie", {
            "cookie": "SESSDATA=x; DedeUserID=111", "at": 1, "source": "ext",
        }, origin=web_ui._EXTENSION_ORIGIN)
        assert result["ok"] is True
    finally:
        panel.stop()


# ── uid 前缀碰撞：12345 不能命中 123456 ─────────────────────────────────


def test_cookie_for_uid_requires_exact_dedeuserid(monkeypatch, tmp_path):
    bili = _bili()
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "_MAIN_COOKIE_FILE", "main_cookie.txt")
    monkeypatch.setattr(bili, "_YUI_COOKIE_FILE", "yui_cookie.txt")
    (tmp_path / "main_cookie.txt").write_text(
        bili._COOKIE_FILE_PLAIN_MAGIC + "SESSDATA=x; DedeUserID=123456",
        encoding="utf-8",
    )
    current = bili._cookie_for_uid(123456)
    assert "DedeUserID=123456" in current
    assert bili._cookie_for_uid(12345) == ""


def test_web_ui_cookie_target_requires_exact_uid(monkeypatch, tmp_path):
    bili = _bili()
    web_ui = _web()
    monkeypatch.setattr(web_ui, "_COOKIE_DIR", str(tmp_path))
    (tmp_path / "main_cookie.txt").write_text(
        bili._COOKIE_FILE_PLAIN_MAGIC + "SESSDATA=x; DedeUserID=123456",
        encoding="utf-8",
    )
    assert web_ui._cookie_target_for_uid("12345") is None
    assert web_ui._cookie_target_for_uid("123456") == "main_cookie.txt"


# ── accounts 写失败：不能继续报 bound=True ───────────────────────────────


def test_assign_cookie_reports_failure_when_accounts_write_fails(monkeypatch):
    pkg = _pkg()
    bili = _bili()
    inst = _inst()
    monkeypatch.setattr(pkg, "_read_accounts", lambda: {})
    monkeypatch.setattr(pkg, "_write_accounts", lambda accounts: False)
    monkeypatch.setattr(bili, "write_main_cookie_file", lambda cookie: None)
    result = inst._assign_cookie_locked("SESSDATA=x; DedeUserID=111", 1, "manual", 111)
    assert result["ok"] is False
    assert result["bound"] is False
    assert "写入失败" in result["detail"]


# ── 验真单飞：BaseException 也必须释放 flight ────────────────────────────


def test_verifier_releases_flight_on_baseexception():
    from plugin.plugins.bili_viewer import cookie_validation as cv

    class Fatal(BaseException):
        pass

    verifier = cv.CookieVerifier(
        lambda cookie, timeout: (_ for _ in ()).throw(Fatal("boom")),
        max_attempts=1,
    )
    with pytest.raises(Fatal):
        verifier.verify("SESSDATA=fatal")
    assert verifier._flights == {}
    # flight 释放后，同一个 Cookie 还能重新验真，不会永久 VERIFY_IN_PROGRESS。
    verifier._validator = lambda cookie, timeout: 42
    result = verifier.verify("SESSDATA=fatal")
    assert result.status == cv.VERIFIED
    assert result.uid == 42


# ── 写请求读响应阶段：OSError/SSL 也必须归 WRITE_OUTCOME_UNKNOWN ──────────


def test_read_response_write_oserror_is_unknown_outcome():
    bili = _bili()

    class Resp:
        def read(self):
            raise ssl.SSLError("EOF during read")

    with pytest.raises(bili.BiliError) as exc:
        bili._read_response(Resp(), 1.0, write=True)
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"

    with pytest.raises(bili.BiliError) as exc:
        bili._read_response(Resp(), 1.0, write=False)
    assert exc.value.code == "NETWORK"


# ── release_limit：限流文件损坏时只 warning，不覆盖原始写失败 ────────────


def test_release_limit_does_not_raise_when_ratelimit_load_fails(monkeypatch, caplog):
    from plugin.plugins.bili_viewer import bili, bili_social

    bili_social._RL_PENDING[("yui", "dm")] = [1.0]

    def broken():
        raise bili.BiliError("限流状态损坏", "RATE_STATE_UNAVAILABLE")

    monkeypatch.setattr(bili_social, "_load_ratelimit", broken)
    caplog.set_level(logging.WARNING)
    bili_social.release_limit("dm", 1, "yui")
    assert bili_social._RL_PENDING.get(("yui", "dm")) in (None, [])
    assert any("release_limit 跳过" in r.getMessage() for r in caplog.records)


# ── LOCALAPPDATA 为空：不能拼出相对 CWD 的 ../ 路径 ──────────────────────


def test_localappdata_empty_falls_back_to_home_for_cookie_paths(monkeypatch, tmp_path):
    from plugin.plugins.bili_viewer import cdp_cookie, cookie_auto

    home = tmp_path / "home"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda value: str(home) if value == "~" else value,
    )
    expected = str(home / "AppData" / "Local")
    assert cdp_cookie._local_app_data_root() == expected
    assert cookie_auto._local_app_data_root() == expected


def test_find_asr_python_skips_relative_candidate_when_localappdata_empty(monkeypatch, tmp_path):
    pkg = _pkg()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("BILI_ASR_PYTHON", raising=False)
    monkeypatch.setattr(
        pkg,
        "Path",
        type("FakePath", (), {"home": staticmethod(lambda: home)}),
    )
    seen = []
    monkeypatch.setattr(pkg.os.path, "isfile", lambda path: seen.append(path) or False)
    monkeypatch.setattr(pkg.shutil, "which", lambda name: None)
    assert _inst()._find_asr_python() is None
    expected = str(home / "N.E.K.O" / ".venv" / "Scripts" / "python.exe")
    assert seen == [expected]
