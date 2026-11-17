"""v0.9.44 增量深审回归：全部离线，不访问真实 B站、扩展或浏览器。

对应 A708 报告的三条新发现：
1. _fetch_raw / video_subtitle 读响应体没走 _read_response（死分支 + 裸异常穿透）；
2. web_ui 的 _core_cookie/_refresh_ext_cookie/_cookie_target_for_uid 死代码簇已删除；
3. bili_set_roles 交换大小号时第一份文件写完、第二份抛 OSError 不回滚。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket

import pytest

MAIN_COOKIE = "SESSDATA=main; DedeUserID=111; bili_jct=mj"
YUI_COOKIE = "SESSDATA=yui; DedeUserID=222; bili_jct=yj"


def _bili():
    from plugin.plugins.bili_viewer import bili

    return bili


def _pkg():
    import plugin.plugins.bili_viewer as pkg

    return pkg


def _inst():
    from plugin.plugins.bili_viewer import BiliViewerPlugin

    return BiliViewerPlugin(ctx=None)


class _FakeResp:
    """urlopen 返回值替身：read() 抛指定异常，模拟读阶段超时/半包。"""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def read(self) -> bytes:
        raise self._exc

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args) -> bool:
        return False


# ── 1. _fetch_raw：读阶段异常必须归成 BiliError，不能裸穿透 ────────────────


@pytest.mark.parametrize(
    "exc, code",
    [
        (socket.timeout("timed out"), "TIMEOUT"),
        (http.client.IncompleteRead(b"partial", 100), "NETWORK"),
        (ConnectionResetError(10054, "reset"), "NETWORK"),
    ],
)
def test_fetch_raw_wraps_read_stage_errors(monkeypatch, exc, code):
    bili = _bili()
    monkeypatch.setattr(bili, "_open_with_retry", lambda req, timeout, **kw: _FakeResp(exc))
    with pytest.raises(bili.BiliError) as caught:
        bili._fetch_raw("https://api.bilibili.com/x/v2/dm/web/seg.so", {"oid": 1})
    assert caught.value.code == code


def test_fetch_raw_no_longer_has_dead_http_branches(monkeypatch):
    """_open_with_retry 已把 HTTP/网络异常转成 BiliError，_fetch_raw 不该再吞它。"""
    bili = _bili()

    def boom(req, timeout, **kw):
        raise bili.BiliError("接口 HTTP 412", "RISK_CONTROL")

    monkeypatch.setattr(bili, "_open_with_retry", boom)
    with pytest.raises(bili.BiliError) as caught:
        bili._fetch_raw("https://api.bilibili.com/x/v2/dm/web/seg.so", {"oid": 1})
    # 旧实现会把 412 覆盖成 HTTP_ERROR，风控语义丢失。
    assert caught.value.code == "RISK_CONTROL"


# ── 2. video_subtitle：读阶段走 _read_response；非 JSON 仍是 SUBTITLE_FAIL ─


def _stub_video_info(bili, monkeypatch):
    monkeypatch.setattr(
        bili,
        "video_info",
        lambda bvid: {
            "pages": [{"cid": 7}],
            "subtitle_urls": [{"lan": "zh-CN", "url": "//sub.example/x.json"}],
        },
    )


def test_video_subtitle_wraps_read_stage_timeout(monkeypatch):
    bili = _bili()
    _stub_video_info(bili, monkeypatch)
    monkeypatch.setattr(bili, "_open_with_retry", lambda req, timeout, **kw: _FakeResp(socket.timeout("t")))
    with pytest.raises(bili.BiliError) as caught:
        bili.video_subtitle("BV1xx411c7mD")
    assert caught.value.code == "TIMEOUT"


def test_video_subtitle_wraps_incomplete_read(monkeypatch):
    bili = _bili()
    _stub_video_info(bili, monkeypatch)
    monkeypatch.setattr(
        bili,
        "_open_with_retry",
        lambda req, timeout, **kw: _FakeResp(http.client.IncompleteRead(b"{", 10)),
    )
    with pytest.raises(bili.BiliError) as caught:
        bili.video_subtitle("BV1xx411c7mD")
    assert caught.value.code == "NETWORK"


def test_video_subtitle_non_json_is_still_subtitle_fail(monkeypatch):
    bili = _bili()
    _stub_video_info(bili, monkeypatch)

    class _Plain:
        def read(self) -> bytes:
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    monkeypatch.setattr(bili, "_open_with_retry", lambda req, timeout, **kw: _Plain())
    with pytest.raises(bili.BiliError) as caught:
        bili.video_subtitle("BV1xx411c7mD")
    assert caught.value.code == "SUBTITLE_FAIL"


def test_video_subtitle_parses_body(monkeypatch):
    bili = _bili()
    _stub_video_info(bili, monkeypatch)
    payload = json.dumps({"body": [{"content": "第一句"}, {"content": "  "}, {"content": "第二句"}]}).encode()

    class _Json:
        def read(self) -> bytes:
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    monkeypatch.setattr(bili, "_open_with_retry", lambda req, timeout, **kw: _Json())
    assert bili.video_subtitle("BV1xx411c7mD") == "第一句\n第二句"


# ── 3. bili_set_roles：交换中途失败必须回滚，不留下"只换一半" ─────────────


def _prepare_two_accounts(monkeypatch, tmp_path):
    bili = _bili()
    pkg = _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _inst()
    inst._assign_cookie(MAIN_COOKIE, 0, "extension")
    inst._assign_cookie(YUI_COOKIE, 0, "extension")
    return bili, pkg, inst


def test_set_roles_half_swap_failure_rolls_back(monkeypatch, tmp_path):
    bili, pkg, inst = _prepare_two_accounts(monkeypatch, tmp_path)
    files = {"main": MAIN_COOKIE, "yui": YUI_COOKIE}
    calls: list[tuple[str, str]] = []
    accounts_writes: list[dict] = []

    def yui_writer(cookie):
        calls.append(("yui", cookie))
        files["yui"] = cookie

    def main_writer(cookie):
        calls.append(("main", cookie))
        # 交换的第二笔失败：此时 yui_cookie.txt 已经是主号 Cookie。
        if cookie == YUI_COOKIE:
            raise OSError(28, "disk full")
        files["main"] = cookie

    monkeypatch.setattr(bili, "write_yui_cookie_file", yui_writer)
    monkeypatch.setattr(bili, "write_main_cookie_file", main_writer)
    monkeypatch.setattr(pkg, "_write_accounts", lambda accounts: (accounts_writes.append(accounts), False)[1])
    env_before = (os.environ.get("BILI_COOKIE"), os.environ.get("BILI_YUI_COOKIE"))

    out = asyncio.run(inst.bili_set_roles(action="swap"))
    assert out["is_error"] is True
    assert out["error"] == "COOKIE_WRITE_FAILED"
    # 两份文件必须回到原值，不能停在"只换了一半"。
    assert files == {"main": MAIN_COOKIE, "yui": YUI_COOKIE}
    # 角色表没被改写（第二次写失败就退出了，根本没走到 accounts）。
    assert accounts_writes == []
    # 运行期 env 也没动。
    assert (os.environ.get("BILI_COOKIE"), os.environ.get("BILI_YUI_COOKIE")) == env_before
    # 第一笔成功（yui 拿到主号）、第二笔失败、随后两次回滚。
    assert calls[0] == ("yui", MAIN_COOKIE)
    assert calls[1] == ("main", YUI_COOKIE)


def test_set_roles_swap_still_works(monkeypatch, tmp_path):
    """回滚保护不能改变正常交换的既有行为。"""
    bili, pkg, inst = _prepare_two_accounts(monkeypatch, tmp_path)
    out = asyncio.run(inst.bili_set_roles(action="swap"))
    assert out.get("is_error") is False
    assert bili._cookie("main") == YUI_COOKIE
    assert bili._cookie("yui") == MAIN_COOKIE
    accounts = pkg._read_accounts()
    assert accounts["main"]["uid"] == 222
    assert accounts["yui"]["uid"] == 111
