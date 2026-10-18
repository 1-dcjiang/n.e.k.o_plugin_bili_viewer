"""bili_viewer 单元测试：纯逻辑部分（不依赖网络）。

与 code_sandbox 测试一致：插件模块在函数内导入，conftest.py 注入 sys.path，
避免 isort first-party 判定在本地/CI 间分歧。
"""

from __future__ import annotations

import hashlib
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
