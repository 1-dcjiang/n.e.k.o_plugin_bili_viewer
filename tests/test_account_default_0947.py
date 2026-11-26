"""v0.9.47 单账号默认主号回归：全部离线，不访问真实 B站/扩展。"""

from __future__ import annotations

import asyncio

MAIN_COOKIE = "SESSDATA=main; DedeUserID=111; bili_jct=mj"


def _bili():
    from plugin.plugins.bili_viewer import bili

    return bili


def _pkg():
    import plugin.plugins.bili_viewer as pkg

    return pkg


def _inst():
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    return BiliViewerPlugin(ctx=None)


def test_omitted_account_defaults_to_main_without_yui(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE if role == "main" else "")
    inst = _inst()

    assert inst._resolve_account("") == "main"
    assert inst._resolve_account("main") == "main"
    # 显式 yui 不做静默回退：由具体工具决定报错还是继续校验。
    assert inst._resolve_account("yui") == "yui"


def test_omitted_account_prefers_yui_when_bound(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(
        bili,
        "_cookie",
        lambda role: MAIN_COOKIE if role in ("main", "yui") else "",
    )
    inst = _inst()

    assert inst._resolve_account("") == "yui"
    assert inst._resolve_account("main") == "main"


def test_follow_defaults_to_main_without_yui(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE if role == "main" else "")
    calls = []

    def fake_modify_relation(target, action="follow", account="yui"):
        calls.append((target, action, account))
        return {"account": account, "uid": 12345}

    monkeypatch.setattr(bili, "modify_relation", fake_modify_relation)
    inst = _inst()

    out = asyncio.run(inst.bili_follow_user(target="12345"))

    assert out["is_error"] is False
    assert out["output"] == "已关注（主账号）。"
    assert calls == [("12345", "follow", "main")]


def test_enter_live_explicit_yui_requires_cookie(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE if role == "main" else "")
    inst = _inst()

    out = asyncio.run(inst.bili_enter_live(room="12345", account="yui"))

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"
