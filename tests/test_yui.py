"""bili_viewer v0.7.0：YUI 独立小号（凭证隔离/默认账号回退/节流/评论目标/投稿安全/动态上限/自主唤醒冷却）。

与 test_bili.py 一致：插件模块在函数内导入，conftest.py 注入 sys.path；
全部用 monkeypatch 模拟网络与宿主上下文，不依赖真实网络。
"""

from __future__ import annotations

import asyncio


def _make_inst(monkeypatch) -> "object":
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    return BiliViewerPlugin(ctx=None)


def _fake_post(url, form, timeout=12.0, account="main"):
    return {"code": 0, "data": {}}


# ══════════ 凭证隔离：main / yui 两套 Cookie ══════════


def test_cookie_account_segregation(monkeypatch, tmp_path) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=main1; bili_jct=mainj")
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    assert bili._cookie("main") == "SESSDATA=main1; bili_jct=mainj"
    assert bili._cookie("yui") == ""  # 未配置小号

    # 小号文件注入（可注入目录）
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    (tmp_path / "yui_cookie.txt").write_text("SESSDATA=yui9; bili_jct=yuij", encoding="utf-8")
    assert bili._cookie("yui") == "SESSDATA=yui9; bili_jct=yuij"

    # 环境变量优先于文件
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=yui8; bili_jct=yui8j")
    assert bili._cookie("yui") == "SESSDATA=yui8; bili_jct=yui8j"


def test_yui_require_login_message(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", "/nonexistent-dir")
    try:
        bili.live_send_danmaku("12345", "hi", account="yui")
        raise AssertionError("should raise")
    except bili.BiliError as exc:
        assert exc.code == "NEED_LOGIN"
        assert "YUI" in exc.message


# ══════════ 默认账号回退：有 yui 用小号，无 yui 回退主账号 ══════════


def test_default_account_fallback_to_main(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=m; bili_jct=mj")
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", "/nonexistent-dir")
    captured: list[str] = []

    def fake_send(room, text, account="main"):
        captured.append(account)
        return {"ok": True}

    monkeypatch.setattr(bili, "live_send_danmaku", fake_send)
    inst = _make_inst(monkeypatch)
    out = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="测试"))
    assert out.get("is_error") is False
    assert captured == ["main"]  # 未配置小号 → 回退主账号


def test_default_account_uses_yui_when_configured(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=m; bili_jct=mj")
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    captured: list[str] = []

    def fake_send(room, text, account="main"):
        captured.append(account)
        return {"ok": True}

    monkeypatch.setattr(bili, "live_send_danmaku", fake_send)
    inst = _make_inst(monkeypatch)
    out = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="测试"))
    assert out.get("is_error") is False
    assert captured == ["yui"]  # 配置了小号 → 默认小号


# ══════════ 节流：同目标 60s 内限 1 条弹幕（小号维度） ══════════


def test_yui_danmaku_rate_limit(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")

    def fake_send(room, text, account="main"):
        return {"ok": True}

    monkeypatch.setattr(bili, "live_send_danmaku", fake_send)
    inst = _make_inst(monkeypatch)
    r1 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="第一条", account="yui"))
    assert r1.get("is_error") is False
    r2 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="第二条", account="yui"))
    assert r2.get("is_error") is True
    assert r2.get("error") == "RATE_LIMITED"
    # 主账号不受小号节流影响
    r3 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="主号", account="main"))
    assert r3.get("is_error") is False


def test_yui_comment_rate_limit(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.setattr(bili, "add_reply", lambda oid, t, text, account="yui": {"ok": True})
    monkeypatch.setattr(bili, "video_info", lambda bvid: {"aid": 123456, "title": "t"})
    inst = _make_inst(monkeypatch)
    r1 = asyncio.run(inst.bili_reply_comment(target="BV1xx411c7mD", text="写得好", account="yui"))
    assert r1.get("is_error") is False
    r2 = asyncio.run(inst.bili_reply_comment(target="BV1xx411c7mD", text="再来一条", account="yui"))
    assert r2.get("error") == "RATE_LIMITED"
    # 不同视频不受限
    monkeypatch.setattr(bili, "video_info", lambda bvid: {"aid": 999, "title": "t2"})
    r3 = asyncio.run(inst.bili_reply_comment(target="BV1xx411c7mE", text="另一个", account="yui"))
    assert r3.get("is_error") is False


# ══════════ 评论目标解析 ══════════


def test_reply_comment_no_target(monkeypatch) -> None:
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    inst = _make_inst(monkeypatch)
    # 无 target 且感知不到内容
    monkeypatch.setattr(inst, "_ext_tab", lambda: None)
    out = asyncio.run(inst.bili_reply_comment(text="评论", account="yui"))
    assert out.get("error") == "NO_TARGET"


def test_reply_comment_video_aid_resolution(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    seen: list[tuple] = []

    def fake_add(oid, t, text, account="yui"):
        seen.append((oid, t, text))
        return {"ok": True}

    monkeypatch.setattr(bili, "add_reply", fake_add)
    monkeypatch.setattr(bili, "video_info", lambda bvid: {"aid": 7777, "title": "t"})
    inst = _make_inst(monkeypatch)
    out = asyncio.run(inst.bili_reply_comment(target="https://www.bilibili.com/video/BV1xx411c7mD", text="顶一个", account="yui"))
    assert out.get("is_error") is False
    assert seen == [(7777, 1, "顶一个")]  # 视频评论：oid=aid, type=1


# ══════════ 动态日上限 ══════════


def test_yui_dynamic_daily_cap(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.setattr(bili, "post_dynamic", lambda text, account="yui": {"ok": True})
    inst = _make_inst(monkeypatch)
    ok_count = 0
    for i in range(7):
        out = asyncio.run(inst.bili_yui_dynamic(text=f"动态{i}"))
        if out.get("is_error") is False:
            ok_count += 1
        else:
            assert out.get("error") == "RATE_LIMITED"
    assert ok_count == 6  # 每日上限 6 条


# ══════════ 投稿安全：仅接受 yui_uploads 目录内文件 ══════════


def test_yui_upload_path_safety(monkeypatch) -> None:
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    inst = _make_inst(monkeypatch)
    # 路径穿越拒绝
    out = asyncio.run(inst.bili_yui_upload(file="../evil.mp4", title="t"))
    assert out.get("error") == "BAD_PATH"
    out2 = asyncio.run(inst.bili_yui_upload(file="C:\\windows\\evil.mp4", title="t"))
    assert out2.get("error") == "BAD_PATH"
    # 目录内不存在 → NO_FILE
    out3 = asyncio.run(inst.bili_yui_upload(file="ok.mp4", title="t"))
    assert out3.get("error") == "NO_FILE"


# ══════════ 自主唤醒冷却 + 后台静默（read 不抢界面） ══════════


class _FakeCtx:
    def __init__(self):
        self.pushed: list[dict] = []

    def push_message(self, **kwargs):
        self.pushed.append(kwargs)


def test_autonomy_tick_cooldown(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.delenv("YUI_AUTONOMY", raising=False)
    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    # 用户在看 B站直播 → 跟随
    parsed = {"is_bili": True, "kind": "live", "room_id": "12345", "subject": "星际争霸2比赛"}
    monkeypatch.setattr(inst, "_yui_resolve_target", lambda p: ("live", "12345", {"title": "星际争霸2比赛", "source": "follow"}))
    monkeypatch.setattr(inst, "_yui_autonomy_text", lambda k, t, i: f"自主任务 {k}:{t}")
    inst._yui_autonomy_tick(parsed)
    assert len(fake_ctx.pushed) == 1
    # 冷却期内不再推送
    inst._yui_autonomy_tick(parsed)
    assert len(fake_ctx.pushed) == 1
    # 冷却过期后再次推送
    inst._yui_cool["live:12345"] = 0
    inst._yui_autonomy_tick(parsed)
    assert len(fake_ctx.pushed) == 2


def test_autonomy_push_is_background_silent(monkeypatch) -> None:
    """v0.7.1 后台化：自主唤醒必须 read（不触发 YUI 开口/不弹气泡）、低优先级、带 autonomy 标记。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.delenv("YUI_AUTONOMY", raising=False)
    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    parsed = {"is_bili": True, "kind": "video", "bvid": "BV1xx411c7mD", "subject": "测试视频"}
    monkeypatch.setattr(inst, "_yui_resolve_target", lambda p: ("video", "BV1xx411c7mD", {"title": "测试视频", "source": "follow"}))
    monkeypatch.setattr(inst, "_yui_autonomy_text", lambda k, t, i: "自主任务")
    inst._yui_autonomy_tick(parsed)
    assert len(fake_ctx.pushed) == 1
    kw = fake_ctx.pushed[0]
    assert kw.get("ai_behavior") == "read"  # 静默注入，绝不 respond
    assert kw.get("priority") == 0
    assert kw.get("metadata", {}).get("autonomy") is True
    assert kw.get("visibility") == []
    assert kw.get("coalesce_key") == "yui_autonomy"


def test_autonomy_disabled_env(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.setenv("YUI_AUTONOMY", "0")
    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    inst._yui_autonomy_tick({"is_bili": True, "kind": "live", "room_id": "1", "subject": "s"})
    assert fake_ctx.pushed == []


# ══════════ bili 层：评论/动态写操作 ══════════


def test_bili_add_reply_posts(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    posted: list[tuple] = []

    def fake_post(url, form, timeout=12.0, account="main"):
        posted.append((url, form.get("oid"), form.get("type"), form.get("message")))
        return {"code": 0, "data": {}}

    monkeypatch.setattr(bili, "_http_post", fake_post)
    r = bili.add_reply(7777, 1, "好活", account="yui")
    assert r["ok"] is True
    assert posted == [("https://api.bilibili.com/x/v2/reply/add", 7777, 1, "好活")]


def test_bili_post_dynamic_requires_login(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", "/nonexistent-dir")
    try:
        bili.post_dynamic("测试动态", account="yui")
        raise AssertionError("should raise")
    except bili.BiliError as exc:
        assert exc.code == "NEED_LOGIN"
