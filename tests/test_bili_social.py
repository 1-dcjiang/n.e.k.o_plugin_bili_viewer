"""bili_social（v0.8.0 bili-cli 精华移植）离线单元测试：护栏 / 解析 / 工具注册。

与 test_cdp_cookie.py 一致：模块在函数内导入，避免 pytest 收集期环境差异。
"""

from __future__ import annotations

import json
import os

import pytest


def _social():
    from plugin.plugins.bili_viewer import bili_social

    return bili_social


def _bili():
    from plugin.plugins.bili_viewer import bili

    return bili


@pytest.fixture(autouse=True)
def social_dir(tmp_path):
    """把限流/审计/设备ID 状态目录隔离到临时目录，避免污染真实状态与测试互踩。"""
    env = os.environ.get("BILI_SOCIAL_DIR", "")
    os.environ["BILI_SOCIAL_DIR"] = str(tmp_path)
    yield tmp_path
    if env:
        os.environ["BILI_SOCIAL_DIR"] = env
    else:
        os.environ.pop("BILI_SOCIAL_DIR", None)


# ══════════ 护栏：限流 ══════════


def test_check_limit_allows_and_blocks_interval(social_dir):
    bili_social = _social()
    bili = _bili()
    bili_social.record_limit("dm", {"uid": 1}, "ok", account="yui")
    with pytest.raises(bili.BiliError) as ei:
        bili_social.check_limit("dm", account="yui")
    assert ei.value.code == "RATE_LIMITED"


def test_check_limit_daily_coin_cap(social_dir):
    bili_social = _social()
    bili = _bili()
    # coin 每日 ≤5：连续 5 次成功后再检查应被拒
    for _ in range(5):
        bili_social.record_limit("coin", {"coins": 1}, "ok", units=1, account="yui")
    with pytest.raises(bili.BiliError) as ei:
        bili_social.check_limit("coin", units=1, account="yui")
    assert ei.value.code == "RATE_LIMITED"
    # 不同账号独立计数
    bili_social.check_limit("coin", units=1, account="main")


def test_record_limit_writes_audit(social_dir):
    bili_social = _social()
    bili_social.record_limit("dm", {"uid": 42}, {"ok": True}, account="yui")
    with open(os.path.join(str(social_dir), "audit.log"), encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    assert lines[0]["action"] == "dm"
    assert lines[0]["params"] == {"uid": 42}


def test_device_id_stable(social_dir):
    bili_social = _social()
    a = bili_social._device_id()
    b = bili_social._device_id()
    assert a == b and len(a) >= 16


# ══════════ 护栏：错误码表 ══════════


def test_classify_bili_code():
    bili_social = _social()
    assert bili_social.classify_bili_code(-101) == ("AUTH_FAILED", "登录态失效，请重新配置 Cookie")
    assert bili_social.classify_bili_code(-412)[0] == "RISK_CONTROL"
    assert bili_social.classify_bili_code(12051)[0] == "RISK_CONTROL"
    assert bili_social.classify_bili_code(21007)[0] == "BIZ_ERROR"
    assert bili_social.classify_bili_code(36703)[0] == "RATE_LIMITED"
    assert bili_social.classify_bili_code(999999)[0].startswith("BIZ_")


def test_guard_exc_remap():
    bili_social = _social()
    bili = _bili()
    exc = bili.BiliError("接口错误", "BIZ_-412")
    remapped = bili_social._guard_exc(exc)
    assert remapped.code == "RISK_CONTROL"
    # 非 BIZ_ 原样保留
    orig = bili.BiliError("未登录", "AUTH_FAILED")
    assert bili_social._guard_exc(orig) is orig


# ══════════ 解析辅助 ══════════


def test_notice_item_reply_with():
    bili_social = _social()
    raw = {
        "id": 100,
        "reply_time": 1700000000,
        "user": {"mid": 88, "nickname": "路人"},
        "item": {
            "business_id": 1,
            "source_id": 12345,
            "root_id": 100,
            "source_content": "写得不错",
            "target_reply_content": "谢谢支持",
            "title": "我的视频标题",
            "uri": "https://www.bilibili.com/video/BV1xx411c7mD",
            "subject_id": 999,
        },
    }
    n = bili_social._notice_item(raw, "reply_time", True)
    assert n["user"]["name"] == "路人"
    assert n["on"]["bvid"] == "BV1xx411c7mD"
    assert n["rpid"] == 12345
    assert "bili_reply_comment" in n["reply_with"]
    assert "12345" in n["reply_with"]


def test_dm_content_types():
    bili_social = _social()
    text = bili_social._dm_content({"msg_type": 1, "content": json.dumps({"content": "你好"})})
    assert text == "你好"
    img = bili_social._dm_content({"msg_type": 2, "content": json.dumps({"url": "http://x/y.png"})})
    assert "[图片]" in img
    share = bili_social._dm_content({"msg_type": 7, "content": json.dumps({"title": "猫", "bvid": "BV1xx"})})
    assert "[分享视频]" in share and "BV1xx" in share
    # 非法 JSON 直接原样返回
    assert bili_social._dm_content({"msg_type": 1, "content": "不是json"}) == "不是json"


def test_notice_item_business_type_string():
    """business_id 为字符串时也要正确映射类型（防漏映射）。"""
    bili_social = _social()
    raw = {
        "id": 1,
        "reply_time": 1700000000,
        "user": {"mid": 9, "nickname": "A"},
        "item": {
            "business_id": "1",
            "source_id": 55,
            "source_content": "x",
            "uri": "https://www.bilibili.com/video/BV1xx411c7mD",
            "subject_id": 7,
        },
    }
    n = bili_social._notice_item(raw, "reply_time", True)
    assert n["on"]["type"] == "video"
    assert n["on"]["bvid"] == "BV1xx411c7mD"
    assert n["reply_with"].startswith("bili_reply_comment target=BV1xx411c7mD parent=55")


def test_resolve_dm_uid_digit():
    bili_social = _social()
    assert bili_social._resolve_dm_uid("123456789", "yui") == 123456789


def test_limit_table_complete():
    bili_social = _social()
    # 新模块引用的 action 都必须出现在 LIMITS 表中
    for action in ("comment", "danmaku", "video_like", "favorite", "coin", "dm"):
        assert action in bili_social.LIMITS


def test_wbi_sign_shape():
    """wbi 签名结果必须带 wts + w_rid（fetch_session_msgs/send_msg 依赖）。"""
    bili = _bili()
    mixin = "0" * 32
    signed = bili._wbi_sign({"talker_id": 123, "session_type": 1}, mixin)
    assert "wts" in signed and "w_rid" in signed
    assert signed["w_rid"] == bili._wbi_sign({"talker_id": 123, "session_type": 1}, mixin)["w_rid"]
    # 参数顺序不影响签名结果（内部排序）
    signed2 = bili._wbi_sign({"session_type": 1, "talker_id": 123}, mixin)
    assert signed["w_rid"] == signed2["w_rid"]


def test_wbi_only_endpoints_use_signing():
    """wbi:true 端点（fetch_session_msgs/send_msg）必须走 _wbi_params 签名，不能裸调。"""
    bili_social = _social()
    with open(bili_social.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "_FETCH_MSGS_URL,\n        _wbi_params(" in src or "_FETCH_MSGS_URL, _wbi_params(" in src
    assert "_SEND_MSG_URL," in src and "_wbi_params({" in src.split("_SEND_MSG_URL")[-1][:400]
    # 创作中心接口 verify-only：不得 wbi 签名
    assert "_OVERVIEW_URL" in src and "_wbi_get_account(_OVERVIEW_URL" not in src


# ══════════ 工具注册（__init__.py） ══════════


def test_tools_registered():
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    cls = BiliViewerPlugin
    tools = set(m for m in dir(cls) if m.startswith("bili_"))
    expected = {
        "bili_inbox", "bili_unread", "bili_feed_replies", "bili_feed_mentions",
        "bili_feed_likes", "bili_feed_system", "bili_dm_list", "bili_dm_read",
        "bili_dm_send", "bili_video_relation", "bili_video_like", "bili_video_coin",
        "bili_video_fav", "bili_video_triple", "bili_my_favs", "bili_rank", "bili_my_stats",
    }
    assert expected <= tools
