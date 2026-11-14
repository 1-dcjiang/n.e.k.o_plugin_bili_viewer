"""私信回读三态回归（v0.9.37 复核第 2 项）。

回读接口抛 BiliError 时必须返回 None（无法确认），不能返回 False（未命中），
否则上层会以为没发出去而诱导重复发送。
"""

from __future__ import annotations

import pytest


def test_dm_verified_returns_none_on_read_error(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili, bili_social

    def boom(*_a, **_kw):
        raise bili.BiliError("回读被风控", "RISK")

    monkeypatch.setattr(bili_social, "dm_messages", boom)
    assert bili_social._dm_verified("yui", 42, "你好") is None


def test_dm_verified_false_when_read_ok_but_missing(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili_social

    monkeypatch.setattr(
        bili_social,
        "dm_messages",
        lambda *_a, **_kw: {"messages": [{"from": "me", "text": "别的内容"}]},
    )
    assert bili_social._dm_verified("yui", 42, "你好", tries=1) is False


def test_dm_verified_retries_after_read_error(monkeypatch) -> None:
    """v0.9.38：一次回读异常不再直接放弃，后续重试成功应返回 True。"""
    import time as _real_time

    from plugin.plugins.bili_viewer import bili, bili_social

    class _ScopedTime:
        """只替换被测模块的 time，避免污染全进程的 stdlib 时钟。"""

        sleep = staticmethod(lambda _s: None)

        def __getattr__(self, name):
            return getattr(_real_time, name)

    monkeypatch.setattr(bili_social, "time", _ScopedTime())
    calls = {"n": 0}

    def flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise bili.BiliError("回读被风控", "RISK")
        return {"messages": [{"from": "me", "text": "你好", "ts": 2000}]}

    monkeypatch.setattr(bili_social, "dm_messages", flaky)
    assert bili_social._dm_verified("yui", 42, "你好", since=1990) is True
    assert calls["n"] == 2


def test_dm_verified_ignores_old_same_text(monkeypatch) -> None:
    """v0.9.38：历史上同内容的旧消息不能证明本次发送成功。"""
    from plugin.plugins.bili_viewer import bili_social

    monkeypatch.setattr(
        bili_social,
        "dm_messages",
        lambda *_a, **_kw: {"messages": [{"from": "me", "text": "你好", "ts": 1000}]},
    )
    assert bili_social._dm_verified("yui", 42, "你好", tries=1, since=2000) is False


def test_dm_send_read_error_reports_unverified_without_retry(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili, bili_social

    monkeypatch.setattr(bili, "_require_login", lambda _account: None)
    monkeypatch.setattr(bili, "cookie_uid", lambda _cookie: 1)
    monkeypatch.setattr(bili_social, "_resolve_dm_uid", lambda _uid, _account: 42)
    monkeypatch.setattr(bili_social, "_may_message", lambda _a, _u: "follower")
    monkeypatch.setattr(bili_social, "check_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(bili, "_http_post", lambda *_a, **_kw: {"code": 0})
    monkeypatch.setattr(bili_social, "_dm_verified", lambda *_a, **_kw: None)
    bili_social._dm_last_sends.clear()

    with pytest.raises(bili.BiliError) as exc:
        bili_social.dm_send(account="yui", uid="42", text="测试回读异常")
    assert exc.value.code == "DM_UNVERIFIED"
    assert "回读接口异常" in exc.value.message
    assert "严禁重复发送" in exc.value.message
