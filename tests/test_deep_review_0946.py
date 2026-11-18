"""A795 增量深审回归：全部离线，不访问真实 B站、扩展或浏览器。

覆盖：
1. 大小号互换全程持有 _ACCOUNTS_LOCK，且同步文件操作不在事件循环线程；
2. 角色表写失败后回滚也失败时，不再谎报“大小号未互换”；
3. 绑定流程 Cookie 落盘失败时 strict 抛错，不继续提交角色表；
4. _read_response 拒绝超大响应体；
5. _fetch_raw 拒绝 gzip 解压炸弹；
6. cdp_cookie._http_json 用 with 关闭连接。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading

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


def _prepare_two_accounts(monkeypatch, tmp_path):
    bili, pkg = _bili(), _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    inst = _inst()
    with pkg._ACCOUNTS_LOCK:
        inst._assign_cookie_locked(MAIN_COOKIE, 0, "extension", 111)
        inst._assign_cookie_locked(YUI_COOKIE, 0, "extension", 222)
    return bili, pkg, inst


def test_set_roles_holds_lock_and_runs_off_event_loop(monkeypatch, tmp_path):
    bili, pkg, inst = _prepare_two_accounts(monkeypatch, tmp_path)
    observed: dict = {}
    orig = pkg._swap_cookie_files

    def wrapped(main_cookie, yui_cookie):
        observed["owned"] = pkg._ACCOUNTS_LOCK._is_owned()
        observed["thread"] = threading.get_ident()
        return orig(main_cookie, yui_cookie)

    monkeypatch.setattr(pkg, "_swap_cookie_files", wrapped)
    out = asyncio.run(inst.bili_set_roles(action="swap"))

    assert out["is_error"] is False and out["result"]["swapped"] is True
    assert observed["owned"] is True
    assert observed["thread"] != threading.get_ident()
    assert bili._main_cookie_from_file() == YUI_COOKIE
    assert bili._yui_cookie_from_file() == MAIN_COOKIE


def test_set_roles_reports_unrecoverable_rollback(monkeypatch, tmp_path):
    _b, pkg, inst = _prepare_two_accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(pkg, "_write_accounts", lambda _accounts: False)
    monkeypatch.setattr(pkg, "_restore_cookie_files", lambda _main, _yui: False)

    out = asyncio.run(inst.bili_set_roles(action="swap"))

    assert out["is_error"] is True
    assert out["error"] == "ACCOUNTS_WRITE_FAILED"
    assert "回滚也失败" in out["output"]
    assert "可能已互换" in out["output"]
    assert "大小号未互换" not in out["output"]


def test_binding_cookie_write_failure_does_not_commit_accounts(monkeypatch, tmp_path):
    bili, pkg = _bili(), _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))

    def fail(_path, _cookie):
        raise OSError(28, "disk full")

    monkeypatch.setattr(bili, "_write_cookie_file", fail)
    inst = _inst()
    with pkg._ACCOUNTS_LOCK:
        out = inst._assign_cookie_locked(MAIN_COOKIE, 0, "extension", 111)

    assert out["ok"] is False and out["bound"] is False
    assert "Cookie 文件写入失败" in out["detail"]
    assert pkg._read_accounts() == {}


def test_read_response_rejects_oversize_body():
    bili = _bili()

    class Resp:
        def read(self, size=-1):
            assert size == bili._MAX_RESPONSE_BYTES + 1
            return b"x" * size

    try:
        bili._read_response(Resp(), 1.0)
    except bili.BiliError as exc:
        assert exc.code == "RESPONSE_TOO_LARGE"
    else:
        raise AssertionError("oversize response must be rejected")

    try:
        bili._read_response(Resp(), 1.0, write=True)
    except bili.BiliError as exc:
        assert exc.code == "WRITE_OUTCOME_UNKNOWN"
    else:
        raise AssertionError("oversize write response must be unknown outcome")


def test_fetch_raw_rejects_gzip_bomb(monkeypatch):
    bili = _bili()
    bomb = gzip.compress(b"x" * (bili._MAX_RESPONSE_BYTES + 1))

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self, *_args):
            return bomb

    monkeypatch.setattr(bili, "_open_with_retry", lambda *_a, **_k: Resp())
    try:
        bili._fetch_raw("https://api.bilibili.com/x/test", {})
    except bili.BiliError as exc:
        assert exc.code == "DANMAKU_PARSE"
        assert "上限" in str(exc)
    else:
        raise AssertionError("gzip bomb must be rejected")


def test_cdp_http_json_closes_response(monkeypatch):
    from plugin.plugins.bili_viewer import cdp_cookie

    closed: list[bool] = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            closed.append(True)

        def read(self, *_args):
            return json.dumps({"Browser": "ok"}).encode()

    monkeypatch.setattr(
        cdp_cookie.urllib.request, "urlopen", lambda *_a, **_k: Resp(),
    )
    assert cdp_cookie._http_json("http://127.0.0.1:9222/json/version") == {"Browser": "ok"}
    assert closed == [True]
