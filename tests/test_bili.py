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
    assert t is not None and "前台窗口推断" in t
    t2 = _sync_text({"kind": "app", "is_bili": False, "app": "steam.exe", "subject": "赛博朋克"})
    assert t2 is not None and "前台窗口推断" in t2


def test_watcher_not_started_without_host(monkeypatch) -> None:
    """无宿主 ctx（测试环境）不启动后台线程。"""
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
    inst = BiliViewerPlugin(ctx=None)
    assert inst._sync_thread is None


def test_watcher_not_started_when_disabled(monkeypatch) -> None:
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    monkeypatch.setenv("BILLI_SYNC_DISABLE", "1")
    inst = BiliViewerPlugin(ctx=None)
    assert inst._sync_thread is None
    monkeypatch.delenv("BILLI_SYNC_DISABLE", raising=False)
