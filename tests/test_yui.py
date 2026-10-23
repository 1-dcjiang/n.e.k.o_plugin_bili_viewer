"""bili_viewer v0.7.0：YUI 独立小号（凭证隔离/默认账号回退/节流/评论目标/投稿安全/动态上限/自主唤醒冷却）。

与 test_bili.py 一致：插件模块在函数内导入，conftest.py 注入 sys.path；
全部用 monkeypatch 模拟网络与宿主上下文，不依赖真实网络。
"""

from __future__ import annotations

import asyncio
import os


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


# ══════════ 双账号归位（v0.7.10）：第一个登录=大号，第二个登录=小号，可后期互换 ══════════


def test_main_cookie_file_fallback(monkeypatch, tmp_path) -> None:
    """v0.7.10：主账号 Cookie 也支持插件目录 main_cookie.txt（绑定持久化）。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    (tmp_path / "main_cookie.txt").write_text("SESSDATA=mainf; bili_jct=mainfj", encoding="utf-8")
    assert bili._cookie("main") == "SESSDATA=mainf; bili_jct=mainfj"
    # env 优先于文件
    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=maine; bili_jct=mainej")
    assert bili._cookie("main") == "SESSDATA=maine; bili_jct=mainej"


def test_cookie_uid_parse(monkeypatch) -> None:
    """v0.7.10：从 Cookie 解析 uid（DedeUserID，无需网络）。"""
    from plugin.plugins.bili_viewer import bili

    assert bili.cookie_uid("SESSDATA=x; DedeUserID=123456; bili_jct=y") == 123456
    assert bili.cookie_uid("SESSDATA=x; DedeUserID=88; bili_jct=y") == 88
    assert bili.cookie_uid("SESSDATA=x; bili_jct=y") is None


def _pkg() -> "object":
    """包模块即 __init__.py（BiliViewerPlugin 的全局命名空间）。"""
    import plugin.plugins.bili_viewer as _pkg_mod

    return _pkg_mod


def test_assign_cookie_first_is_main_second_is_yui(monkeypatch, tmp_path) -> None:
    """v0.7.10：按登录顺序归位——第一个登录的账号=大号，第二个=小号（默认操作账号）。"""
    from plugin.plugins.bili_viewer import bili

    pkg = _pkg()

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _make_inst(monkeypatch)

    # 第一个登录（uid=111）
    r1 = inst._assign_cookie("SESSDATA=a; DedeUserID=111; bili_jct=a1", 0, "extension")
    assert r1["bound"] is True and "大号" in r1["detail"]
    assert bili._cookie("main") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"
    assert (tmp_path / "main_cookie.txt").read_text(encoding="utf-8") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"
    acc = pkg._read_accounts()
    assert acc["main"]["uid"] == 111

    # 第二个登录（uid=222）→ 自动成为小号
    r2 = inst._assign_cookie("SESSDATA=b; DedeUserID=222; bili_jct=b2", 0, "extension")
    assert r2["bound"] is True and "小号" in r2["detail"]
    assert bili._cookie("yui") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"
    assert (tmp_path / "yui_cookie.txt").read_text(encoding="utf-8") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"
    acc = pkg._read_accounts()
    assert acc["yui"]["uid"] == 222

    # 重复推送同一 uid → 刷新不新增
    r3 = inst._assign_cookie("SESSDATA=b2; DedeUserID=222; bili_jct=b2b", 1, "extension")
    assert r3["bound"] is True and "小号" in r3["detail"]
    acc = pkg._read_accounts()
    assert set(acc) == {"main", "yui"}
    assert acc["yui"]["uid"] == 222

    # 第三个账号 → 拒绝（大小号都已绑定）
    r4 = inst._assign_cookie("SESSDATA=c; DedeUserID=333; bili_jct=c3", 0, "extension")
    assert r4["bound"] is False
    assert bili._cookie("yui") == "SESSDATA=b2; DedeUserID=222; bili_jct=b2b"  # 未被覆盖
    assert bili._cookie("main") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"


def test_set_roles_swap(monkeypatch, tmp_path) -> None:
    """v0.7.10：后期说明修改——交换大小号（文件互换 + 角色映射互换 + env 同步）。"""
    from plugin.plugins.bili_viewer import bili

    pkg = _pkg()

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _make_inst(monkeypatch)
    inst._assign_cookie("SESSDATA=a; DedeUserID=111; bili_jct=a1", 0, "extension")
    inst._assign_cookie("SESSDATA=b; DedeUserID=222; bili_jct=b2", 0, "extension")

    out = asyncio.run(inst.bili_set_roles(action="swap"))
    assert out.get("is_error") is False
    # 文件互换：原小号（uid222）现在是大号
    assert bili._cookie("main") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"
    assert bili._cookie("yui") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"
    acc = pkg._read_accounts()
    assert acc["main"]["uid"] == 222
    assert acc["yui"]["uid"] == 111
    # 运行期 env 同步
    assert os.environ.get("BILI_COOKIE") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"
    assert os.environ.get("BILI_YUI_COOKIE") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"


def test_set_roles_by_uid(monkeypatch, tmp_path) -> None:
    """v0.7.10：按 uid 把账号设为指定角色。"""
    from plugin.plugins.bili_viewer import bili

    pkg = _pkg()

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _make_inst(monkeypatch)
    inst._assign_cookie("SESSDATA=a; DedeUserID=111; bili_jct=a1", 0, "extension")
    inst._assign_cookie("SESSDATA=b; DedeUserID=222; bili_jct=b2", 0, "extension")
    # 把 uid=222 设为小号（它当前是大号 → 等价互换）
    out = asyncio.run(inst.bili_set_roles(uid=222, role="yui"))
    assert out.get("is_error") is False
    assert bili._cookie("yui") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"
    assert bili._cookie("main") == "SESSDATA=a; DedeUserID=111; bili_jct=a1"
    # 再设回大号
    out2 = asyncio.run(inst.bili_set_roles(uid=222, role="main"))
    assert out2.get("is_error") is False
    assert bili._cookie("main") == "SESSDATA=b; DedeUserID=222; bili_jct=b2"


def test_set_roles_env_locked(monkeypatch, tmp_path) -> None:
    """v0.7.10：env 覆盖文件层的账号无法互换（避免误清空）。"""
    from plugin.plugins.bili_viewer import bili

    pkg = _pkg()

    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _make_inst(monkeypatch)
    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=a; DedeUserID=111; bili_jct=a1")
    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=b; DedeUserID=222; bili_jct=b2")
    out = asyncio.run(inst.bili_set_roles(action="swap"))
    assert out.get("error") == "ENV_LOCKED"
    # 文件层账号可互换
    (tmp_path / "main_cookie.txt").write_text("SESSDATA=a; DedeUserID=111; bili_jct=a1", encoding="utf-8")
    (tmp_path / "yui_cookie.txt").write_text("SESSDATA=b; DedeUserID=222; bili_jct=b2", encoding="utf-8")
    out2 = asyncio.run(inst.bili_set_roles(action="swap"))
    assert out2.get("is_error") is False


def test_bili_accounts_view(monkeypatch, tmp_path) -> None:
    """v0.7.10：YUI 分清大小号——bili_accounts 返回双账号视图与默认角色。"""
    from plugin.plugins.bili_viewer import bili

    pkg = _pkg()

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _make_inst(monkeypatch)
    inst._assign_cookie("SESSDATA=a; DedeUserID=111; bili_jct=a1", 0, "extension")
    inst._assign_cookie("SESSDATA=b; DedeUserID=222; bili_jct=b2", 0, "extension")
    monkeypatch.setattr(bili, "my_info", lambda role: {"uname": "大号名" if role == "main" else "小号名", "mid": 111 if role == "main" else 222, "level": 6})
    out = asyncio.run(inst.bili_accounts())
    assert out.get("is_error") is False
    assert "大号" in out["output"] and "小号" in out["output"]
    assert "默认使用" in out["output"] and "yui" in out["output"]
    assert out["result"]["main"]["uid"] == 111
    assert out["result"]["yui"]["uid"] == 222
    assert out["result"]["default_role"] == "yui"


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


def test_yui_rate_reserve_atomic_blocks_concurrent(monkeypatch) -> None:
    """v0.7.8 复查优化：reserve 原子占用——并发第二个请求在同窗口内被拒绝，
    且发送失败后 rollback 释放窗口允许重试。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.setattr(bili, "live_send_danmaku", lambda room, text, account="main": {"ok": True})
    inst = _make_inst(monkeypatch)
    # 第一次发送：成功（占用窗口）
    r1 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="第一条", account="yui"))
    assert r1.get("is_error") is False
    # 第二次同目标：即使不加 dedup（换内容），窗口内仍被拒
    r2 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="第二条不同内容", account="yui"))
    assert r2.get("error") == "RATE_LIMITED"


def test_yui_rate_rollback_after_failure(monkeypatch) -> None:
    """v0.7.8 复查优化：发送失败回退占用，稍后可重试（不被误限流）。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    state = {"fail": True}

    def fake_send(room, text, account="main"):
        if state["fail"]:
            raise bili.BiliError("接口失败", "SEND_FAILED")
        return {"ok": True}

    monkeypatch.setattr(bili, "live_send_danmaku", fake_send)
    inst = _make_inst(monkeypatch)
    r1 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="失败", account="yui"))
    assert r1.get("is_error") is True
    # 失败已回退，立即重试成功（不在窗口内）
    state["fail"] = False
    r2 = asyncio.run(inst.bili_live_send_danmaku(room="12345", text="成功", account="yui"))
    assert r2.get("is_error") is False


def test_yui_upload_mutex_blocks_second(monkeypatch) -> None:
    """v0.7.8 复查优化：投稿互斥——第二个并发投稿返回 BUSY，不跑双流程。"""
    import os as _os

    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    monkeypatch.setattr(_os.path, "isfile", lambda p: True)  # 绕过真实文件检查
    calls: list[str] = []

    def fake_submit(file_path, title, desc="", tags="", tid=171, account="yui"):
        calls.append(title)
        return {"ok": True}

    monkeypatch.setattr(bili, "submit_video", fake_submit)
    inst = _make_inst(monkeypatch)
    # 手动占住上传互斥（模拟自主引擎正在上传）
    with inst._yui_upload_lock:
        inst._yui_uploading = True
    r = asyncio.run(inst.bili_yui_upload(file="v.mp4", title="第二个"))
    inst._yui_uploading = False
    assert r.get("error") == "BUSY"
    assert calls == []  # 第二个流程没有启动


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


def test_yui_dynamic_failure_releases_daily_quota(monkeypatch) -> None:
    """v0.7.8 复查优化：动态发布失败回退天级占用，稍后可重试。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    state = {"fail": True}

    def fake_dyn(text, account="yui"):
        if state["fail"]:
            raise bili.BiliError("接口失败", "DYN_FAILED")
        return {"ok": True}

    monkeypatch.setattr(bili, "post_dynamic", fake_dyn)
    inst = _make_inst(monkeypatch)
    r1 = asyncio.run(inst.bili_yui_dynamic(text="失败的动态"))
    assert r1.get("is_error") is True
    state["fail"] = False
    r2 = asyncio.run(inst.bili_yui_dynamic(text="重试的动态"))
    assert r2.get("is_error") is False


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


def test_autonomy_popular_cache_throttle(monkeypatch) -> None:
    """v0.7.1 复查优化：无跟随目标时 popular 拉取 60s 缓存，轮询不重复打 B站 API。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili

    calls = {"n": 0}

    def fake_popular(limit=3):
        calls["n"] += 1
        return [{"bvid": "BV1xx411c7mD", "title": "热门视频", "uname": "u"}]

    monkeypatch.setattr(bili, "popular", fake_popular)
    inst = BiliViewerPlugin(ctx=None)
    k1, t1, i1 = inst._yui_resolve_target(None)
    assert (k1, t1) == ("video", "BV1xx411c7mD")
    k2, t2, _ = inst._yui_resolve_target(None)
    assert (k2, t2) == ("video", "BV1xx411c7mD")
    assert calls["n"] == 1  # 第二次命中缓存
    inst._yui_popular_ts = 0  # 缓存过期
    inst._yui_resolve_target(None)
    assert calls["n"] == 2


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


def test_bili_post_dynamic_wbi_signed_query(monkeypatch) -> None:
    """v0.7.3 复查修复：动态发布必须带 wbi 签名 query（wts+w_rid），否则 B站 -403/-352。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    calls: list[tuple[str, dict, str]] = []

    def fake_post_json(url, body, csrf, account="yui", timeout=12.0, query=None):
        calls.append((url, dict(body or {}), dict(query or {})))
        return {"code": 0, "data": {}}

    monkeypatch.setattr(bili, "_http_post_json", fake_post_json)
    monkeypatch.setattr(bili, "my_info", lambda account="main": {"mid": 123456})
    monkeypatch.setattr(bili, "_get_mixin_key", lambda: "m" * 32)
    r = bili.post_dynamic("测试动态", account="yui")
    assert r["ok"] is True
    assert len(calls) == 1
    url, body, query_dict = calls[0]
    assert "/x/dynamic/web/create/v1/dyn" in url
    assert body["content"]["content"] == "测试动态"
    assert body["up_uid"] == 123456
    # query 必须含 csrf 与 wbi 签名结果
    assert query_dict.get("csrf") == "yj"
    assert "wts" in query_dict
    assert "w_rid" in query_dict


def test_bili_safe_multipart_name(monkeypatch) -> None:
    """v0.7.3 防御：multipart 文件名清洗，引号/换行/反斜杠不破坏 boundary。"""
    from plugin.plugins.bili_viewer import bili

    assert bili._safe_multipart_name('a"b') == "a_b"
    assert bili._safe_multipart_name("a\r\nb") == "a__b"
    assert bili._safe_multipart_name("a\\b") == "a_b"
    assert bili._safe_multipart_name("ok.mp4") == "ok.mp4"


# ══════════ 自主查看私信（v0.8.2） ══════════


def test_unread_tick_push_and_cooldown(monkeypatch) -> None:
    """自主查私信：有未读且冷却已过时静默注入；无未读/冷却内不注入。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili_social

    monkeypatch.setenv("BILI_YUI_COOKIE", "SESSDATA=y; bili_jct=yj")
    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    fake_counts = {
        "replies": 1, "mentions": 0, "likes": 0, "private_messages": 2, "system": 1,
    }
    monkeypatch.setattr(bili_social, "unread_counts", lambda account="yui": fake_counts)
    # 有未读 → 注入
    inst._unread_tick()
    assert len(fake_ctx.pushed) == 1
    kw = fake_ctx.pushed[0]
    assert kw.get("ai_behavior") == "read"
    assert kw.get("visibility") == []
    assert kw.get("coalesce_key") == "bili_unread_sync"
    assert "私信2" in kw["parts"][0]["text"]
    assert "4 条未读" in kw["parts"][0]["text"]
    # 冷却期内不重复注入
    inst._unread_tick()
    assert len(fake_ctx.pushed) == 1
    # 无未读 → 不注入
    monkeypatch.setattr(bili_social, "unread_counts", lambda account="yui": {"replies": 0, "mentions": 0, "likes": 0, "private_messages": 0, "system": 0})
    inst._unread_inject_ts = 0.0
    inst._unread_tick()
    assert len(fake_ctx.pushed) == 1


# ══════════ 弹幕@自主回复（v0.8.5）：带房间号可自主回弹幕，同直播间冷却 ══════════


def test_handle_mention_auto_reply_with_room(monkeypatch) -> None:
    """@消息带房间号：注入自主回复任务（bili_live_send_danmaku），静默 read。"""
    import time

    from plugin.plugins.bili_viewer import BiliViewerPlugin

    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    inst._handle_mention({"at": time.time(), "text": "@yui小猫娘 你好呀", "sender": "路人A", "room": "12345"})
    assert len(fake_ctx.pushed) == 1
    kw = fake_ctx.pushed[0]
    assert kw.get("ai_behavior") == "read"
    assert kw.get("coalesce_key") == "danmaku_mention"
    assert kw.get("metadata", {}).get("mention_auto_reply") is True
    assert "bili_live_send_danmaku" in kw["parts"][0]["text"]
    assert "房间 12345" in kw["parts"][0]["text"]


def test_handle_mention_cooldown_same_room(monkeypatch) -> None:
    """同一直播间冷却期内再次@：只提醒不催自主回复（防刷屏）。"""
    import time

    from plugin.plugins.bili_viewer import BiliViewerPlugin

    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    m = {"at": time.time(), "text": "@小猫娘 在吗", "sender": "B", "room": "12345"}
    inst._handle_mention(m)
    inst._handle_mention(m)  # 冷却期内
    assert len(fake_ctx.pushed) == 2
    assert fake_ctx.pushed[0]["metadata"]["mention_auto_reply"] is True
    assert fake_ctx.pushed[1]["metadata"]["mention_auto_reply"] is False
    # 冷却过期后恢复自主回复
    inst._mention_reply_cool["mention_reply:12345"] = 0.0
    inst._handle_mention(m)
    assert fake_ctx.pushed[2]["metadata"]["mention_auto_reply"] is True


def test_handle_mention_no_room_only_remind(monkeypatch) -> None:
    """无房间号（扩展未带）：只注入提醒，不催自主回复。"""
    import time

    from plugin.plugins.bili_viewer import BiliViewerPlugin

    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    inst._handle_mention({"at": time.time(), "text": "yui 快看", "sender": "C", "room": ""})
    assert len(fake_ctx.pushed) == 1
    assert fake_ctx.pushed[0]["metadata"]["mention_auto_reply"] is False
    assert "下次自然对话" in fake_ctx.pushed[0]["parts"][0]["text"]


def test_handle_mention_stale_skipped(monkeypatch) -> None:
    """超过 30 秒的旧@：不注入（感知循环已消费或过时）。"""
    import time

    from plugin.plugins.bili_viewer import BiliViewerPlugin

    fake_ctx = _FakeCtx()
    inst = BiliViewerPlugin(ctx=fake_ctx)
    inst._handle_mention({"at": time.time() - 40, "text": "@yui 你好", "sender": "D", "room": "9"})
    assert len(fake_ctx.pushed) == 0


def test_mention_notify_room_roundtrip(monkeypatch) -> None:
    """web_ui 的@通知 room 字段写入与 clear 清空（v0.8.5 新增字段）。"""
    import web_ui

    old = web_ui._MENTION_NOTIFY
    try:
        web_ui._MENTION_NOTIFY = {"at": 1.0, "text": "@小猫娘 在吗", "sender": "E", "room": "888"}
        info = web_ui.get_mention_notify(clear=True)
        assert info["room"] == "888"
        assert web_ui._MENTION_NOTIFY["text"] == ""
        assert web_ui._MENTION_NOTIFY["sender"] == ""
        assert web_ui._MENTION_NOTIFY["room"] == ""
    finally:
        web_ui._MENTION_NOTIFY = old


def test_yui_cookie_file_mtime_cache(monkeypatch, tmp_path) -> None:
    """v0.7.3 优化：小号 Cookie 文件 mtime 缓存，内容未变不重复读盘。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    bili._yui_cookie_file_cache = (0.0, "")  # 清全局缓存
    (tmp_path / "yui_cookie.txt").write_text("SESSDATA=abc; bili_jct=def", encoding="utf-8")
    # 确定性 mtime：1.0 期间内容不变 → 只读一次；2.0 视为文件已改 → 重读
    mtimes = iter([1.0, 1.0, 1.0, 2.0, 2.0])
    monkeypatch.setattr(bili.os.path, "getmtime", lambda _p: next(mtimes))
    reads = {"n": 0}
    import builtins

    orig_open = builtins.open

    def counting_open(path, *a, **kw):
        if str(path).endswith("yui_cookie.txt"):
            reads["n"] += 1
        return orig_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert bili._cookie("yui") == "SESSDATA=abc; bili_jct=def"
    assert bili._cookie("yui") == "SESSDATA=abc; bili_jct=def"  # 命中缓存，不重复读
    assert reads["n"] == 1
    assert bili._cookie("yui") == "SESSDATA=abc; bili_jct=def"  # mtime 仍 1.0，继续命中
    assert reads["n"] == 1
    # mtime 变化（文件被改）→ 重读
    assert bili._cookie("yui") == "SESSDATA=abc; bili_jct=def"  # 内容文件未真正改，重读仍同值
    assert reads["n"] == 2
