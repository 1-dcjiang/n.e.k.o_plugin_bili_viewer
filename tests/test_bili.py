"""bili_viewer 单元测试：纯逻辑部分（不依赖网络）。

与 code_sandbox 测试一致：插件模块在函数内导入，conftest.py 注入 sys.path，
避免 isort first-party 判定在本地/CI 间分歧。
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib

# ══════════ BV 号解析 ══════════


def test_parse_bvid() -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, _parse_bvid

    assert _parse_bvid("BV1xx411c7mD") == "BV1xx411c7mD"
    assert _parse_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=2") == "BV1xx411c7mD"
    try:
        _parse_bvid("av12345")
        raise AssertionError("should raise")
    except BiliError:
        pass
    try:
        _parse_bvid("随便一个链接")
        raise AssertionError("should raise")
    except BiliError:
        pass


# ══════════ 时长/时间格式化 ══════════


def test_fmt_duration() -> None:
    from plugin.plugins.bili_viewer.bili import _fmt_duration

    assert _fmt_duration(0) == "0:00"
    assert _fmt_duration(65) == "1:05"
    assert _fmt_duration(3661) == "1:01:01"
    assert _fmt_duration(None) == "0:00"


def test_fmt_time() -> None:
    from plugin.plugins.bili_viewer.bili import _fmt_time

    assert "2020" in _fmt_time(1577836800)
    assert _fmt_time(0) != ""


# ══════════ wbi 签名 ══════════


def test_wbi_sign() -> None:
    from plugin.plugins.bili_viewer.bili import _wbi_sign

    mixin = "a" * 32
    params = {"keyword": "测试", "page": 1}
    signed = _wbi_sign(params, mixin)
    assert "wts" in signed
    assert "w_rid" in signed
    assert len(signed["w_rid"]) == 32
    # 校验 md5：排序后的 query + mixin
    items = sorted((k, str(v)) for k, v in signed.items() if k != "w_rid")
    import urllib.parse

    query = urllib.parse.urlencode(items)
    expect = hashlib.md5((query + mixin).encode()).hexdigest()
    assert signed["w_rid"] == expect


def test_mixin_table_length() -> None:
    from plugin.plugins.bili_viewer.bili import _MIXIN_TABLE

    assert len(_MIXIN_TABLE) == 64
    assert sorted(_MIXIN_TABLE) == list(range(64))


# ══════════ 弹幕协议封包/解包 ══════════


def test_bili_pack_unpack_roundtrip() -> None:
    from plugin.plugins.bili_viewer.bili import _bili_pack, _bili_unpack

    body = b'{"hello":"world"}'
    pkt = _bili_pack(body, op=7, version=1)
    assert len(pkt) == 16 + len(body)
    total, header_len, version, op, seq = struct.unpack_from(">IHHII", pkt, 0)
    assert total == len(pkt) and header_len == 16 and version == 1 and op == 7 and seq == 1
    unpacked = _bili_unpack(pkt)
    assert len(unpacked) == 1
    assert unpacked[0] == (7, 1, body)


def test_bili_unpack_multiple() -> None:
    from plugin.plugins.bili_viewer.bili import _bili_pack, _bili_unpack

    buf = _bili_pack(b"a", op=5, version=0) + _bili_pack(b"b", op=2, version=1)
    out = _bili_unpack(buf)
    assert len(out) == 2
    assert out[0] == (5, 0, b"a")
    assert out[1] == (2, 1, b"b")


def test_danmaku_messages_zlib() -> None:
    from plugin.plugins.bili_viewer.bili import _bili_pack, _danmaku_messages

    inner = _bili_pack('{"cmd":"DANMU_MSG","info":[1,"你好"]}'.encode(), op=5, version=0)
    compressed = zlib.compress(inner)
    outer = _bili_pack(compressed, op=5, version=2)
    msgs = _danmaku_messages(outer)
    assert len(msgs) == 1
    assert msgs[0][0] == 0
    assert b"DANMU_MSG" in msgs[0][1]


def test_danmaku_messages_skips_non5() -> None:
    from plugin.plugins.bili_viewer.bili import _bili_pack, _danmaku_messages

    buf = _bili_pack(b"[]", op=2, version=1)  # 心跳响应
    assert _danmaku_messages(buf) == []


# ══════════ 搜索类型映射 ══════════


def test_search_type_mapping() -> None:
    from plugin.plugins.bili_viewer.bili import search

    # type 映射逻辑不直接导出，验证默认值路径不会崩（keyword 校验）
    try:
        search("", "video")
        raise AssertionError("should raise for empty keyword")
    except Exception as exc:  # noqa: BLE001
        assert "关键词" in str(exc) or "keyword" in str(exc)


def test_wrap_err_shape() -> None:
    from plugin.plugins.bili_viewer import _wrap_err
    from plugin.plugins.bili_viewer.bili import BiliError

    out = _wrap_err(BiliError("测试错误", "TEST_CODE"))
    assert out["is_error"] is True
    assert out["error"] == "TEST_CODE"
    assert "测试错误" in out["output"]


# ══════════ 账号 / 稿件（登录态） ══════════


def test_csrf_extract(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc123; bili_jct=xyz789; other=1")
    assert bili._csrf() == "xyz789"
    monkeypatch.delenv("BILI_COOKIE")
    assert bili._csrf() == ""


def test_require_login(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, _require_login

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    try:
        _require_login()
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "NEED_LOGIN"


def test_edit_video_requires_cookie(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, edit_video

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    try:
        edit_video("BV1xx411c7mD", title="新标题")
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "NEED_LOGIN"


def test_live_send_requires_cookie(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, live_send_danmaku

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    try:
        live_send_danmaku("1", "你好")
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "NEED_LOGIN"


def test_msg_length_validation(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, live_send_danmaku

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; bili_jct=tok")
    try:
        live_send_danmaku("1", "这" * 40)
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "MSG_TOO_LONG"
    monkeypatch.delenv("BILI_COOKIE", raising=False)


def test_receive_ext_dom_stores_realtime(monkeypatch) -> None:
    """扩展页面 DOM 实况推送：存储并 120s 内可读（v0.6.34）。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin, web_ui

    inst = BiliViewerPlugin(ctx=None)
    now = int(__import__("time").time())
    resp = inst._receive_ext_dom({
        "url": "https://live.bilibili.com/1025219",
        "kind": "live",
        "title": "北极僵尸从入门到入土",
        "uname": "干凝冰羽",
        "online": "15",
        "area": "星际争霸",
        "danmaku": [{"user": "观众A", "text": "666"}, {"user": "观众B", "text": "毒爆一个神两个"}],
        "at": now,
    })
    assert resp["ok"] is True
    dom = inst._ext_dom()
    assert dom is not None and dom["kind"] == "live"
    assert dom["title"] == "北极僵尸从入门到入土"
    assert len(dom["danmaku"]) == 2
    # 过期后不可读
    stale = {"kind": "live", "at": now - web_ui._EXT_ONLINE_WINDOW - 10}
    inst._ext_dom_data = stale
    assert inst._ext_dom() is None


def test_receive_ext_shot_stores_and_see_screen(monkeypatch) -> None:
    """扩展截屏推送：落盘、120s 内可读，bili_see_screen 返回画面路径（v0.6.36）。"""
    import asyncio
    import base64

    from plugin.plugins.bili_viewer import BiliViewerPlugin, web_ui

    inst = BiliViewerPlugin(ctx=None)
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0fakejpegdata").decode()
    now = int(__import__("time").time())
    resp = inst._receive_ext_shot({
        "image": f"data:image/jpeg;base64,{jpeg}",
        "url": "https://live.bilibili.com/1025219",
        "title": "北极僵尸从入门到入土",
        "at": now,
    })
    assert resp["ok"] is True
    shot = inst._ext_shot()
    assert shot is not None and shot["title"] == "北极僵尸从入门到入土"
    assert shot["size"] > 0 and os.path.exists(shot["path"])
    out = asyncio.run(inst.bili_see_screen())
    assert out["is_error"] is False and shot["path"] in out["output"]
    # 过期后不可读
    inst._ext_shot_data = {"at": now - web_ui._EXT_ONLINE_WINDOW - 10, "path": shot["path"]}
    out2 = asyncio.run(inst.bili_see_screen())
    assert out2["is_error"] is True and out2["error"] == "NO_SHOT"


def test_render_see_live_and_video() -> None:
    from plugin.plugins.bili_viewer.bili import render_see

    live = render_see({
        "kind": "live", "title": "测试直播间", "uname": "主播A", "status": "直播中",
        "online": 123, "parent_area": "网游", "area": "星际争霸",
        "recent_danmaku": [{"user": "观众1", "text": "666"}],
    })
    assert "测试直播间" in live and "观众1：666" in live and "123人" in live
    video = render_see({
        "kind": "video", "title": "测试视频", "uname": "UP主B", "duration": 125,
        "tname": "游戏", "view": 1000, "recent_danmaku": ["前排"], "subtitle_head": "大家好",
    })
    assert "测试视频" in video and "UP主B" in video and "2:05" in video and "前排" in video and "大家好" in video


def test_live_send_danmaku_uses_msg_send(monkeypatch) -> None:
    """直播弹幕走 /msg/send，roomid 用页面号（不能用 room_init 解析号，v0.6.30）。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; bili_jct=tok")
    posted: dict = {}

    def _fake_post(url, form):
        assert url.endswith("/msg/send")
        posted.update(url=url, form=form)
        return {"code": 0, "message": "0"}

    monkeypatch.setattr(bili, "_http_post", _fake_post)
    try:
        result = bili.live_send_danmaku("live.bilibili.com/1025219", "美味的食材")
    finally:
        monkeypatch.delenv("BILI_COOKIE", raising=False)
    assert result["ok"] is True
    assert result["room_id"] == 1025219
    assert posted["form"]["roomid"] == 1025219
    assert posted["form"]["msg"] == "美味的食材"
    assert posted["form"]["csrf"] == "tok"
    assert posted["form"]["csrf_token"] == "tok"


def test_no_csrf_for_write(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, live_send_danmaku

    # 有 SESSDATA 但缺 bili_jct：应明确提示缺 CSRF
    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc")
    try:
        live_send_danmaku("1", "你好")
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "NO_CSRF"
    monkeypatch.delenv("BILI_COOKIE", raising=False)


def test_video_send_danmaku_requires_cookie(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, send_video_danmaku

    monkeypatch.delenv("BILI_COOKIE", raising=False)
    try:
        send_video_danmaku("BV1xx411c7mD", "你好")
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "NEED_LOGIN"


def test_video_send_danmaku_posts_with_cid(monkeypatch) -> None:
    """视频弹幕：解析第一P cid，带 csrf 表单投递，progress 毫秒换算，WBI 签名。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; bili_jct=tok")
    posted: dict = {}

    def _fake_video_info(bvid):
        assert bvid == "BV1xx411c7mD"
        return {"title": "测试", "pages": [{"cid": 10086}, {"cid": 20086}]}

    def _fake_post(url, form):
        assert url.endswith("/x/v2/dm/post")
        posted.update(url=url, form=form)
        return {"code": 0, "message": "0"}

    monkeypatch.setattr(bili, "video_info", _fake_video_info)
    monkeypatch.setattr(bili, "_http_post", _fake_post)
    monkeypatch.setattr(bili, "_get_mixin_key", lambda: "mixin-key")
    try:
        result = bili.send_video_danmaku("BV1xx411c7mD", "前排围观", progress_ms=45000)
    finally:
        monkeypatch.delenv("BILI_COOKIE", raising=False)
    assert result["ok"] is True
    assert posted["form"]["oid"] == 10086  # 第一P cid
    assert posted["form"]["bvid"] == "BV1xx411c7mD"  # bvid 必填（缺失返回 -400 请求错误）
    assert posted["form"]["msg"] == "前排围观"
    assert posted["form"]["progress"] == 45000
    assert posted["form"]["csrf"] == "tok"
    # WBI 签名：wts 与 w_rid 必须存在（缺失触发 -352 风控）
    assert posted["form"]["wts"] is not None
    assert posted["form"]["w_rid"] is not None


def test_video_send_danmaku_keeps_biz_code(monkeypatch) -> None:
    """视频弹幕：API 业务失败时保留原始 BIZ code（如 BIZ_-352），供面板诊断定位风控。"""
    from plugin.plugins.bili_viewer import bili
    from plugin.plugins.bili_viewer.bili import BiliError

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; bili_jct=tok")

    def _fake_video_info(bvid):
        return {"title": "测试", "pages": [{"cid": 10086}]}

    def _fake_post(url, form):
        raise BiliError("请求错误", "BIZ_-352")

    monkeypatch.setattr(bili, "video_info", _fake_video_info)
    monkeypatch.setattr(bili, "_http_post", _fake_post)
    try:
        send_video_danmaku = bili.send_video_danmaku
        send_video_danmaku("BV1xx411c7mD", "666")
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "SEND_FAILED:BIZ_-352"
    finally:
        monkeypatch.delenv("BILI_COOKIE", raising=False)


def test_video_send_danmaku_msg_limit(monkeypatch) -> None:
    from plugin.plugins.bili_viewer.bili import BiliError, send_video_danmaku

    monkeypatch.setenv("BILI_COOKIE", "SESSDATA=abc; bili_jct=tok")
    try:
        send_video_danmaku("BV1xx411c7mD", "这" * 120)
        raise AssertionError("should raise")
    except BiliError as exc:
        assert exc.code == "MSG_TOO_LONG"
    monkeypatch.delenv("BILI_COOKIE", raising=False)


def test_live_info_fallback_uses_wbi(monkeypatch) -> None:
    """get_info 风控时回退到 getInfoByRoom，且必须带 wbi 签名。"""
    from plugin.plugins.bili_viewer import bili
    from plugin.plugins.bili_viewer.bili import BiliError

    def _raise(*_args, **_kwargs):
        raise BiliError("风控", "RISK_CONTROL")

    called = {"wbi": False}

    def _fake_wbi_get(url, params):
        called["wbi"] = True
        assert "/getInfoByRoom" in url
        return {"code": 0, "data": {"room_info": {"room_id": 1, "title": "测试直播间", "live_status": 1, "online": 99}}}

    monkeypatch.setattr(bili, "_resolve_room", lambda room: {"room_id": 1, "live_status": 1})
    monkeypatch.setattr(bili, "_http_get", _raise)
    monkeypatch.setattr(bili, "_wbi_get", _fake_wbi_get)
    info = bili.live_info("1")
    assert called["wbi"] is True
    assert info["title"] == "测试直播间"
    assert info["status_text"] == "直播中"


def test_live_danmaku_uses_wbi(monkeypatch) -> None:
    """getDanmuInfo 必须带 wbi 签名；连接阶段再走 websocket。"""
    import types

    from plugin.plugins.bili_viewer import bili

    called = {"wbi": False}

    def _fake_wbi_get(url, params):
        called["wbi"] = True
        assert "/getDanmuInfo" in url
        return {"code": 0, "data": {"token": "t0k", "host_list": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]}}

    def _fake_ws_connect(host, port, path, timeout):
        return types.SimpleNamespace(settimeout=lambda _t: None, close=lambda: None)

    def _fake_resolve(room):
        return {"room_id": 1, "live_status": 1}

    monkeypatch.setattr(bili, "_http_get", lambda *a, **k: {"code": 0, "data": {"room_id": 1, "live_status": 1}})
    monkeypatch.setattr(bili, "_wbi_get", _fake_wbi_get)
    monkeypatch.setattr(bili, "_resolve_room", _fake_resolve)
    monkeypatch.setattr(bili, "_ws_connect", _fake_ws_connect)
    monkeypatch.setattr(bili, "_ws_send", lambda *a, **k: None)
    monkeypatch.setattr(bili, "_ws_recv", lambda *a, **k: b"")  # 无消息，直到超时
    try:
        bili.live_danmaku("1", duration=3, limit=5)
        raise AssertionError("should raise NO_DANMAKU")
    except Exception as exc:
        assert getattr(exc, "code", "") == "NO_DANMAKU"
    assert called["wbi"] is True


def test_parse_danmaku_proto() -> None:
    """seg.so protobuf 弹幕解析：文本 field 7、进度 field 15。"""
    from plugin.plugins.bili_viewer.bili import _parse_danmaku_proto

    def _varint(v: int) -> bytes:
        out = b""
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                out += bytes([b | 0x80])
            else:
                out += bytes([b])
                return out

    def _field(no: int, payload: bytes) -> bytes:
        return _varint((no << 3) | 2) + _varint(len(payload)) + payload

    def _vfield(no: int, v: int) -> bytes:
        return _varint((no << 3) | 0) + _varint(v)

    def _elem(progress: int, text: str) -> bytes:
        return _vfield(1, 12345) + _vfield(3, 0) + _field(7, text.encode("utf-8")) + _vfield(15, progress)

    raw = _field(1, _elem(12, "第一条弹幕")) + _field(1, _elem(35, "第二条弹幕")) + _vfield(2, 1)
    items = _parse_danmaku_proto(raw)
    assert items == [(12, "第一条弹幕"), (35, "第二条弹幕")]


def test_video_danmaku_uses_seg_so(monkeypatch) -> None:
    """video_danmaku 应优先走 seg.so protobuf，而非失效的 dm/list.so XML。"""
    from plugin.plugins.bili_viewer import bili

    def _fake_video_info(bvid):
        return {"pages": [{"cid": 1001, "page": 1, "part": "正片"}]}

    def _fake_fetch_raw(url, params):
        assert "/x/v2/dm/web/seg.so" in url
        # 第一段返回 1 条弹幕；后续段返回空（触发采集结束）
        if params["segment_index"] != 1:
            return b""
        assert params == {"type": 1, "oid": 1001, "segment_index": 1}

        def _varint(v):
            out = b""
            while True:
                b = v & 0x7F
                v >>= 7
                if v:
                    out += bytes([b | 0x80])
                else:
                    out += bytes([b])
                    return out

        def _field(no, payload):
            return _varint((no << 3) | 2) + _varint(len(payload)) + payload

        def _vfield(no, v):
            return _varint((no << 3) | 0) + _varint(v)

        elem = _field(7, "测试弹幕".encode()) + _vfield(15, 42)
        return _field(1, elem)

    monkeypatch.setattr(bili, "video_info", _fake_video_info)
    monkeypatch.setattr(bili, "_fetch_raw", _fake_fetch_raw)
    got = bili.video_danmaku("BV1xx411c7mD", limit=5)
    assert got == ["[0:42] 测试弹幕"]


# ══════════ 当前观看感知（通用窗口感知） ══════════


def test_parse_window_title_bili_video() -> None:
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("【原神】4.0 剧情PV_哔哩哔哩_bilibili", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
    assert p["is_bili"] is True
    assert p["kind"] == "video"
    assert p["site"] == "bilibili"
    assert p["is_browser"] is True
    assert p["subject"] == "【原神】4.0 剧情PV"


def test_parse_window_title_bili_live() -> None:
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("某某游戏直播中_哔哩哔哩直播", "chrome.exe")
    assert p["is_bili"] is True and p["kind"] == "live" and p["subject"] == "某某游戏直播中"


def test_parse_window_title_site() -> None:
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("如何评价XXX - 知乎 - Google Chrome", "chrome.exe")
    assert p["is_bili"] is False
    assert p["site"] == "zhihu"
    assert p["kind"] == "web"
    assert "如何评价XXX" in p["subject"]


def test_parse_window_title_app() -> None:
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("工作文档 - Word", "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE")
    assert p["is_bili"] is False
    assert p["kind"] == "app"
    assert p["site"] == ""
    assert p["app"] == "winword.exe"
    assert p["subject"] == "工作文档 - Word"


def test_parse_window_title_empty() -> None:
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("", "")
    assert p["kind"] == "unknown" and p["is_bili"] is False


def test_now_watching_app(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setattr(bili, "_visible_windows", lambda: [{"title": "记事本 - 未命名", "exe": "notepad.exe"}])
    info = bili.now_watching()
    assert info["is_bili"] is False
    assert info["kind"] == "app"
    assert info["app"] == "notepad.exe"


def test_now_watching_bili_search_fails(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili
    from plugin.plugins.bili_viewer.bili import BiliError

    def _raise(*_args, **_kwargs):
        raise BiliError("风控", "RISK_CONTROL")

    monkeypatch.setattr(bili, "_visible_windows", lambda: [{"title": "测试视频_哔哩哔哩_bilibili", "exe": "chrome.exe"}])
    monkeypatch.setattr(bili, "search", _raise)
    info = bili.now_watching()
    assert info["is_bili"] is True and info["kind"] == "video"
    assert "match" not in info and "video" not in info


# ══════════ 扩展标签页精确感知（parse_bili_url / now_watching(tab=)） ══════════


def test_parse_bili_url_video():
    from plugin.plugins.bili_viewer.bili import parse_bili_url

    p = parse_bili_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=2", "原神 PV_哔哩哔哩_bilibili")
    assert p is not None and p["is_bili"] is True
    assert p["kind"] == "video" and p["bvid"] == "BV1GJ411x7h7"
    assert p["subject"] == "原神 PV"


def test_parse_bili_url_live():
    from plugin.plugins.bili_viewer.bili import parse_bili_url

    p = parse_bili_url("https://live.bilibili.com/123456", "直播中：测试-哔哩哔哩直播")
    assert p is not None and p["is_bili"] is True
    assert p["kind"] == "live" and p["room_id"] == "123456"


def test_parse_bili_url_bangumi_and_shortlink():
    from plugin.plugins.bili_viewer.bili import parse_bili_url

    ep = parse_bili_url("https://www.bilibili.com/bangumi/play/ep123456", "番剧_哔哩哔哩_bilibili")
    assert ep is not None and ep["kind"] == "bangumi" and ep["ep"] == "123456"
    b23 = parse_bili_url("https://b23.tv/abc123", "测试")
    assert b23 is not None and b23["is_bili"] is True


def test_parse_bili_url_non_bili_returns_none():
    from plugin.plugins.bili_viewer.bili import parse_bili_url

    assert parse_bili_url("https://www.zhihu.com/question/1", "知乎") is None
    assert parse_bili_url("", "") is None


def test_now_watching_prefers_extension_tab(monkeypatch):
    """扩展标签页优先：bvid 直接精确匹配视频详情，无需窗口/搜索。"""
    from plugin.plugins.bili_viewer import bili

    called = {}

    def _fake_video_info(bvid):
        called["bvid"] = bvid
        return {"title": "精确视频", "owner_name": "UP", "view": 10, "danmaku": 2,
                "duration": 3600, "pubdate": 1700000000, "desc": "描述"}

    monkeypatch.setattr(bili, "video_info", _fake_video_info)
    monkeypatch.setattr(bili, "pick_now_watching", lambda: {"kind": "unknown"})  # 不应被调用
    info = bili.now_watching(tab={"url": "https://www.bilibili.com/video/BV1GJ411x7h7", "title": "精确视频_哔哩哔哩_bilibili", "at": 0})
    assert info["from"] == "extension-tab"
    assert info["kind"] == "video" and info["bvid"] == "BV1GJ411x7h7"
    assert called.get("bvid") == "BV1GJ411x7h7"
    assert info["video"]["title"] == "精确视频"


def test_now_watching_extension_tab_live(monkeypatch):
    """扩展标签页直播：room_id 直接匹配直播间信息。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setattr(bili, "live_info", lambda room: {"title": "测试直播间", "uname": "主播",
                                                         "status_text": "直播中", "online": 88,
                                                         "area": "网游", "parent_area": "游戏"})
    info = bili.now_watching(tab={"url": "https://live.bilibili.com/666", "title": "测试直播间-哔哩哔哩直播", "at": 0})
    assert info["kind"] == "live" and info["room_id"] == "666"
    assert info["live"]["title"] == "测试直播间" and info["live"]["online"] == 88


def test_now_watching_extension_tab_non_bili_uses_title(monkeypatch):
    """非 B站标签页：按标题通用解析（site 识别）。"""
    from plugin.plugins.bili_viewer import bili

    info = bili.now_watching(tab={"url": "https://www.zhihu.com/question/1", "title": "如何评价XX - 知乎 - Google Chrome", "at": 0})
    assert info["from"] == "extension-tab"
    assert info["site"] == "zhihu" and info["is_bili"] is False


# ══════════ 实时感知同步（后台注入） ══════════


def test_sync_text_bili() -> None:
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "video", "is_bili": True, "subject": "原神 PV"})
    assert t is not None and "原神 PV" in t and "B站视频" in t


def test_sync_text_site() -> None:
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "web", "is_bili": False, "site": "zhihu", "subject": "如何评价"})
    assert t is not None and "zhihu" in t and "如何评价" in t


def test_sync_text_unknown() -> None:
    from plugin.plugins.bili_viewer import _sync_text

    assert _sync_text({"kind": "unknown", "is_bili": False}) is None


def test_window_fingerprint() -> None:
    from plugin.plugins.bili_viewer import _window_fingerprint

    a = _window_fingerprint({"app": "chrome.exe", "site": "bilibili", "kind": "video", "subject": "A"})
    b = _window_fingerprint({"app": "chrome.exe", "site": "bilibili", "kind": "video", "subject": "B"})
    assert a != b
    assert _window_fingerprint({"app": "x", "site": "", "kind": "app", "subject": "y"}) != a


def test_is_terminal_window() -> None:
    from plugin.plugins.bili_viewer.bili import _is_terminal_window

    for app in ("windowsterminal.exe", "powershell.exe", "cmd.exe", "python.exe", "pwsh.exe", "wt.exe", "bash.exe"):
        assert _is_terminal_window({"app": app, "kind": "app"}), app
    for app in ("chrome.exe", "msedge.exe", "steam.exe", "WeChat.exe", "explorer.exe", "QQMusic.exe"):
        assert not _is_terminal_window({"app": app, "kind": "app"}), app
    assert not _is_terminal_window({"app": "", "kind": "unknown"})


def test_is_neko_client() -> None:
    from plugin.plugins.bili_viewer.bili import _is_neko_client

    for app in ("N.E.K.O.exe", "n.e.k.o.exe", "neko.exe", "launcher.exe"):
        assert _is_neko_client({"app": app, "kind": "app"}), app
    assert not _is_neko_client({"app": "chrome.exe", "kind": "web"})


def test_skip_window() -> None:
    from plugin.plugins.bili_viewer.bili import _skip_window

    assert _skip_window({"kind": "unknown", "window": "x", "app": ""})
    assert _skip_window({"kind": "app", "window": "", "app": "steam.exe"})
    assert _skip_window({"kind": "app", "window": "term", "app": "powershell.exe"})
    assert _skip_window({"kind": "app", "window": "N.E.K.O", "app": "n.e.k.o.exe"})
    assert not _skip_window({"kind": "web", "window": "视频_哔哩哔哩_bilibili", "app": "chrome.exe"})


def test_window_score() -> None:
    from plugin.plugins.bili_viewer.bili import _window_score

    assert _window_score({"kind": "video", "is_bili": True, "site": "bilibili"}) == 100
    assert _window_score({"kind": "web", "is_bili": False, "site": "zhihu"}) == 90
    assert _window_score({"kind": "web", "is_bili": False, "site": ""}) == 80
    assert _window_score({"kind": "app", "is_bili": False, "site": ""}) == 70
    assert _window_score({"kind": "other", "is_bili": False, "site": ""}) == 60


def test_pick_now_watching(monkeypatch) -> None:
    """多窗口场景：聊天客户端在前台也不影响——后台的 B站视频优先。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setattr(
        bili,
        "_visible_windows",
        lambda: [
            {"title": "N.E.K.O", "exe": r"C:\apps\N.E.K.O.exe"},
            {"title": "Terminal", "exe": r"C:\Windows\System32\WindowsTerminal.exe"},
            {"title": "丧尸爆发合集_游戏热门视频 - Google Chrome", "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe"},
            {"title": "QQ", "exe": r"C:\Program Files\Tencent\QQ\QQ.exe"},
        ],
    )
    got = bili.pick_now_watching()
    assert got is not None
    assert got["app"] == "chrome.exe"
    assert "丧尸爆发合集" in got["subject"]


def test_pick_now_watching_all_skipped(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setattr(
        bili,
        "_visible_windows",
        lambda: [
            {"title": "N.E.K.O", "exe": r"C:\apps\N.E.K.O.exe"},
            {"title": "Terminal", "exe": r"C:\Windows\System32\WindowsTerminal.exe"},
        ],
    )
    assert bili.pick_now_watching() is None


def test_parse_bili_section_suffix() -> None:
    """B站新版视频页标题「视频_分区名」（不带'哔哩哔哩'字样）应识别为 B站视频。"""
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("1939锦标赛事件到底发生了什么？一个视频给你讲清楚_游戏杂谈 - Google Chrome", "chrome.exe")
    assert p["is_bili"] is True and p["kind"] == "video"
    assert p["site"] == "bilibili"
    assert p["subject"] == "1939锦标赛事件到底发生了什么？一个视频给你讲清楚"


def test_parse_watt_toolkit_not_steam() -> None:
    """Steam++（Watt Toolkit）不应被误判为 Steam 站点。"""
    from plugin.plugins.bili_viewer.bili import parse_window_title

    p = parse_window_title("Watt Toolkit", r"D:\Steam++\Steam++.exe")
    assert p["site"] == "" and p["is_bili"] is False and p["kind"] == "app"


def test_pick_prefers_bili_over_watt_toolkit(monkeypatch) -> None:
    """Watt Toolkit 在前台/可见时，B站视频窗口仍应胜出。"""
    from plugin.plugins.bili_viewer import bili

    monkeypatch.setattr(
        bili,
        "_visible_windows",
        lambda: [
            {"title": "Watt Toolkit", "exe": r"D:\Steam++\Steam++.exe"},
            {"title": "1939锦标赛事件到底发生了什么？一个视频给你讲清楚_游戏杂谈 - Google Chrome", "exe": "chrome.exe"},
            {"title": "N.E.K.O", "exe": r"D:\SteamLibrary\steamapps\common\n.e.k.o\N.E.K.O.exe"},
        ],
    )
    got = bili.pick_now_watching()
    assert got is not None and got["site"] == "bilibili" and got["is_bili"] is True


def test_sync_text_prefix_is_inference() -> None:
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "video", "is_bili": True, "subject": "原神 PV"})
    assert t is not None and "内部感知" in t and "B站视频" in t
    t2 = _sync_text({"kind": "app", "is_bili": False, "app": "steam.exe", "subject": "赛博朋克"})
    assert t2 is not None and "实时感知" in t2 and "steam.exe" in t2


def test_sync_text_live_marks_live() -> None:
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "live", "is_bili": True, "subject": "某直播间"})
    assert t is not None and "B站直播" in t


def test_sync_text_live_carries_room_id() -> None:
    """直播感知文本必须带房间号，YUI 才能直接发弹幕/查详情。"""
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "live", "is_bili": True, "subject": "点播我的卡组", "room_id": "1426057300"})
    assert t is not None and "房间号 1426057300" in t and "点播我的卡组" in t


def test_sync_text_video_carries_bvid() -> None:
    """视频感知文本带 BV 号，YUI 可直接查详情/弹幕。"""
    from plugin.plugins.bili_viewer import _sync_text

    t = _sync_text({"kind": "video", "is_bili": True, "subject": "原神 PV", "bvid": "BV1GJ411x7h7"})
    assert t is not None and "BV号 BV1GJ411x7h7" in t


def test_watcher_not_started_without_host(monkeypatch) -> None:
    """无宿主 ctx（测试环境）不启动后台线程。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = BiliViewerPlugin(ctx=None)
    assert inst._sync_thread is None


def test_ext_tab_property_does_not_shadow_method(monkeypatch) -> None:
    """扩展 tab 推送后 _ext_tab() 仍可调用（防止实例属性遮蔽方法的回归）。

    历史 bug：_receive_ext_tab 写 self._ext_tab 属性会遮蔽 def _ext_tab 方法，
    导致 watcher/感知调用 self._ext_tab() 抛 TypeError、感知静默退化。
    """
    import time as _t

    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = BiliViewerPlugin(ctx=None)
    now = int(_t.time())
    inst._receive_ext_tab("https://www.bilibili.com/video/BV1GJ411x7h7", "测试_哔哩哔哩", now)
    tab = inst._ext_tab()
    assert tab is not None
    assert tab["url"].endswith("BV1GJ411x7h7")
    assert tab["title"] == "测试_哔哩哔哩"
    assert tab["at"] == float(now)


def test_ext_tab_expires_after_window(monkeypatch) -> None:
    """超过 120s 新鲜窗口后 _ext_tab() 返回 None。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = BiliViewerPlugin(ctx=None)
    inst._receive_ext_tab("https://www.bilibili.com/video/BV1GJ411x7h7", "测试", int(__import__("time").time()) - 200)
    assert inst._ext_tab() is None


def test_watcher_not_started_when_disabled(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.setenv("BILLI_SYNC_DISABLE", "1")
    inst = BiliViewerPlugin(ctx=None)
    assert inst._sync_thread is None


# ══════════ BILI_COOKIE 配置注入 ══════════


def test_cookie_env_priority() -> None:
    from plugin.plugins.bili_viewer import _apply_cookie_from_config

    cfg = {"settings": {"bili_cookie": "cfg=SESSDATA_CFG"}}
    env = {"BILI_COOKIE": "env=SESSDATA_ENV"}
    assert _apply_cookie_from_config(cfg, env) == "env=SESSDATA_ENV"


def test_cookie_from_config_when_no_env() -> None:
    from plugin.plugins.bili_viewer import _apply_cookie_from_config

    cfg = {"settings": {"bili_cookie": "cfg=SESSDATA_CFG;bili_jct=abc"}}
    assert _apply_cookie_from_config(cfg, {}) == "cfg=SESSDATA_CFG;bili_jct=abc"


def test_cookie_empty_when_unset() -> None:
    from plugin.plugins.bili_viewer import _apply_cookie_from_config

    assert _apply_cookie_from_config({}, {}) == ""
    assert _apply_cookie_from_config({"settings": {"bili_cookie": "   "}}, {}) == ""
    assert _apply_cookie_from_config({"settings": {}}, {"BILI_COOKIE": ""}) == ""


# ══════════ 自动绑定（allow_cookie_scan / cookie_auto） ══════════


def test_allow_cookie_scan_values() -> None:
    from plugin.plugins.bili_viewer import _allow_cookie_scan

    assert _allow_cookie_scan({}) is False
    assert _allow_cookie_scan({"settings": {}}) is False
    assert _allow_cookie_scan({"settings": {"allow_cookie_scan": True}}) is True
    assert _allow_cookie_scan({"settings": {"allow_cookie_scan": "true"}}) is True
    assert _allow_cookie_scan({"settings": {"allow_cookie_scan": "1"}}) is True
    assert _allow_cookie_scan({"settings": {"allow_cookie_scan": False}}) is False
    assert _allow_cookie_scan({"settings": {"allow_cookie_scan": "no"}}) is False


def test_cookie_summary_no_leak() -> None:
    from plugin.plugins.bili_viewer import _cookie_summary

    assert _cookie_summary("") == "未配置"
    assert "SESSDATA" in _cookie_summary("SESSDATA=abc; bili_jct=xyz")
    assert "abc" not in _cookie_summary("SESSDATA=abc; bili_jct=xyz")  # 不泄露值
    assert _cookie_summary("buvid3=xx") == "已配置（无登录态字段）"


def test_cookie_uid_extraction() -> None:
    from plugin.plugins.bili_viewer import _cookie_uid

    assert _cookie_uid("") == ""
    assert _cookie_uid("buvid3=xx") == ""
    assert _cookie_uid("DedeUserID=123456; SESSDATA=a") == "123456"
    assert _cookie_uid("SESSDATA=a; DedeUserID=98765; bili_jct=b") == "98765"
    assert _cookie_uid("DedeUserID=abc") == ""  # 非数字不误取


def test_live_danmaku_duration_clamped(monkeypatch) -> None:
    """工具层 clamp duration 到 [1,30]，与实际采集上限一致。

    真实宿主把 LLM 的 JSON 参数展开为命名参数调用（entry trigger 路径），
    因此这里以命名参数形态调用；`_` 在真实调用中不含 "parameters" 键。
    """
    import asyncio

    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili

    captured: dict = {}
    def fake_live_danmaku(room, duration=8):
        captured["duration"] = duration
        return []

    monkeypatch.setattr(bili, "live_danmaku", fake_live_danmaku)
    inst = BiliViewerPlugin(ctx=None)

    # LLM 传了超大的 duration → 收紧到 30
    loop = asyncio.new_event_loop()
    try:
        r = loop.run_until_complete(inst.bili_live_danmaku(room="1", duration=3600))
    finally:
        loop.close()
    assert captured["duration"] == 30
    assert r["is_error"] is False
    assert "采集 30 秒" in r["output"]

    # LLM 未提供 duration → 默认 8
    captured.clear()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(inst.bili_live_danmaku(room="1", duration=0))
    finally:
        loop.close()
    assert captured["duration"] == 8


def test_build_cookie_string_sanitizes() -> None:
    from plugin.plugins.bili_viewer.cookie_auto import build_cookie_string

    s = build_cookie_string(
        [("SESSDATA", "a"), ("bili_jct", "b"), ("SESSDATA", "dup"), ("bad", "x;y"), ("bad2", 'x"y'), ("ok", "v")]
    )
    assert s == "SESSDATA=a; bili_jct=b; ok=v"


def test_detect_no_browser(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import cookie_auto

    monkeypatch.setenv("LOCALAPPDATA", "Z:\\nonexistent\\dir")
    status, cookie, detail = cookie_auto.detect_bili_cookie()
    assert status == cookie_auto._STATUS_NO_BROWSER and cookie == "" and detail


def test_detect_locked_when_browser_running(monkeypatch, tmp_path) -> None:
    """模拟浏览器进程在运行且 Cookies 文件不可读 → 锁定状态。"""
    from plugin.plugins.bili_viewer import cookie_auto

    monkeypatch.setattr(
        cookie_auto,
        "AUTO_COOKIE_SOURCES",
        (("Chrome", os.path.join("Google", "Chrome", "User Data")),),
    )
    root = tmp_path / "Google" / "Chrome" / "User Data"
    (root / "Default" / "Network").mkdir(parents=True)
    with open(root / "Default" / "Network" / "Cookies", "w", encoding="utf-8") as f:
        f.write("")  # 文件存在但打开被模拟为锁定
    with open(root / "Local State", "w", encoding="utf-8") as f:
        f.write('{"os_crypt":{"encrypted_key":"QQ=="}}')
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cookie_auto, "_load_aes_key", lambda path: b"\x00" * 32)  # 绕过真实 DPAPI
    monkeypatch.setattr(cookie_auto, "_open_cookies_db", lambda path: None)  # 模拟锁
    monkeypatch.setattr(cookie_auto, "_browser_process_running", lambda name: True)  # 浏览器在跑
    status, cookie, _ = cookie_auto.detect_bili_cookie()
    assert status == cookie_auto._STATUS_LOCKED and cookie == ""


def test_detect_db_error_when_browser_closed(monkeypatch, tmp_path) -> None:
    """浏览器进程已退出但 Cookie 库仍打不开 → 数据库异常（fail），不再误报 locked。"""
    from plugin.plugins.bili_viewer import cookie_auto

    monkeypatch.setattr(
        cookie_auto,
        "AUTO_COOKIE_SOURCES",
        (("Chrome", os.path.join("Google", "Chrome", "User Data")),),
    )
    root = tmp_path / "Google" / "Chrome" / "User Data"
    (root / "Default" / "Network").mkdir(parents=True)
    with open(root / "Default" / "Network" / "Cookies", "w", encoding="utf-8") as f:
        f.write("")
    with open(root / "Local State", "w", encoding="utf-8") as f:
        f.write('{"os_crypt":{"encrypted_key":"QQ=="}}')
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cookie_auto, "_load_aes_key", lambda path: b"\x00" * 32)
    monkeypatch.setattr(cookie_auto, "_open_cookies_db", lambda path: None)
    monkeypatch.setattr(cookie_auto, "_browser_process_running", lambda name: False)  # 浏览器已关
    status, cookie, _ = cookie_auto.detect_bili_cookie()
    assert status == cookie_auto._STATUS_FAIL and cookie == ""


def test_live_send_danmaku_dedup_once(monkeypatch) -> None:
    """同一房间同一条弹幕 60 秒内重复触发只发一次（v0.6.38 幂等去重）。"""
    import asyncio

    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(bili, "live_send_danmaku", lambda room, text: (sent.append((room, text)), {"dmid": "D1", "server": "ok"})[1])
    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = object.__new__(BiliViewerPlugin)
    inst._ext_tab = lambda: None

    r1 = asyncio.run(inst.bili_live_send_danmaku(room="1025219", text="主播好帅"))
    assert r1["is_error"] is False and r1["output"] == "弹幕已发送。"
    # 第二次（模拟模型重复触发/兜底入口再次调用）：同房间同内容 → 去重，不再真实发送
    r2 = asyncio.run(inst.bili_live_send_danmaku(room="1025219", text="主播好帅"))
    assert r2["is_error"] is False and r2["output"] == "弹幕已发送。"
    assert r2["result"].get("dedup") is True
    assert len(sent) == 1
    # 不同内容 → 允许再发
    r3 = asyncio.run(inst.bili_live_send_danmaku(room="1025219", text="再来一条"))
    assert r3["is_error"] is False and len(sent) == 2
    assert r3["result"].get("dedup") is None


def test_live_send_danmaku_dedup_expires(monkeypatch) -> None:
    """去重窗口过期后同一弹幕可再次发送。"""
    import asyncio

    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(bili, "live_send_danmaku", lambda room, text: (sent.append((room, text)), {"dmid": "D1", "server": "ok"})[1])
    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = object.__new__(BiliViewerPlugin)
    inst._ext_tab = lambda: None

    asyncio.run(inst.bili_live_send_danmaku(room="1025219", text="过期测试"))
    inst._danmaku_sent.clear()  # 模拟窗口已过（property 只读，clear 内部 dict 即可）
    r = asyncio.run(inst.bili_live_send_danmaku(room="1025219", text="过期测试"))
    assert r["is_error"] is False and r["result"].get("dedup") is None
    assert len(sent) == 2


def test_video_send_danmaku_dedup_once(monkeypatch) -> None:
    """同一视频同一进度同一条弹幕 60 秒内重复触发只发一次。"""
    import asyncio

    from plugin.plugins.bili_viewer import BiliViewerPlugin, bili

    sent: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        bili,
        "send_video_danmaku",
        lambda bvid, text, progress_ms=0: (sent.append((bvid, text, progress_ms)), {"dmid": "D2"})[1],
    )
    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = object.__new__(BiliViewerPlugin)
    inst._ext_tab = lambda: None

    r1 = asyncio.run(inst.bili_video_send_danmaku(bvid="BV1GJ411x7h7", text="镇站之宝", progress=5))
    assert r1["is_error"] is False
    r2 = asyncio.run(inst.bili_video_send_danmaku(bvid="BV1GJ411x7h7", text="镇站之宝", progress=5))
    assert r2["is_error"] is False and r2["result"].get("dedup") is True
    assert len(sent) == 1
    # 同视频不同进度 → 不判重
    r3 = asyncio.run(inst.bili_video_send_danmaku(bvid="BV1GJ411x7h7", text="镇站之宝", progress=30))
    assert r3["is_error"] is False and r3["result"].get("dedup") is None
    assert len(sent) == 2
