"""v0.9.45 联锁复查回归：全部离线，不访问真实 B站、扩展或浏览器。

v0.9.44 的“交换大小号事务化”有个联锁漏洞：`bili.write_main_cookie_file`
与 `write_yui_cookie_file` 内部自己 `except OSError` 只打 warning，
`_swap_cookie_files` 里的 `except OSError` 在真实环境永远进不去——
第二次写失败照样返回 `(True, True)`，上层继续改角色表与 env，
两份 Cookie 停在“只换了一半”。当时 0944 的用例把写函数替换成会抛异常的假货，
所以离线全绿也照不出这个洞。

本文件用**真实写函数**、只让最底层 `_write_cookie_file` 失败，覆盖：
1. 半交换失败会被感知，并按原值回滚；
2. 回滚本身失败时返回 False，不能再谎报“已回滚”；
3. 默认（非 strict）路径保持“写失败不致命”的既有契约；
4. `bili_set_roles` 端到端返回 COOKIE_WRITE_FAILED，且角色表/文件都没动。
"""

from __future__ import annotations

import asyncio
import os

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


def _fail_on_nth_write(bili, monkeypatch, nth: int):
    """保留真实落盘，只让第 ``nth`` 次底层写入抛 OSError（模拟磁盘满/权限）。"""
    orig = bili._write_cookie_file
    calls: list[str] = []

    def flaky(path, cookie):
        calls.append(os.path.basename(path))
        if len(calls) == nth:
            raise OSError(28, "disk full")
        return orig(path, cookie)

    monkeypatch.setattr(bili, "_write_cookie_file", flaky)
    return calls


def test_swap_detects_half_write_with_real_writers(monkeypatch, tmp_path):
    """第二笔写失败必须被 strict 变体抛出，两份文件回到原值。"""
    bili, _pkg_mod, _i = _prepare_two_accounts(monkeypatch, tmp_path)
    calls = _fail_on_nth_write(bili, monkeypatch, 2)

    swapped, rolled_back = _pkg_mod._swap_cookie_files(MAIN_COOKIE, YUI_COOKIE)

    assert (swapped, rolled_back) == (False, True)
    assert bili._main_cookie_from_file() == MAIN_COOKIE
    assert bili._yui_cookie_from_file() == YUI_COOKIE
    # 序列：yui←主号、main←小号(失败)、回滚 main、回滚 yui
    assert calls == ["yui_cookie.txt", "main_cookie.txt", "main_cookie.txt", "yui_cookie.txt"]


def test_restore_reports_unrecoverable_rollback(monkeypatch, tmp_path):
    """回滚两根都写不进去时必须返回 False，不能谎报成功。"""
    bili, _pkg_mod, _i = _prepare_two_accounts(monkeypatch, tmp_path)
    _fail_on_nth_write(bili, monkeypatch, 1)
    assert _pkg_mod._restore_cookie_files(MAIN_COOKIE, YUI_COOKIE) is False


def test_default_writer_keeps_non_fatal_contract(monkeypatch, tmp_path):
    """绑定流程仍按“写失败不致命”处理：不传 strict 就不抛。"""
    bili = _bili()
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    _fail_on_nth_write(bili, monkeypatch, 1)
    assert bili.write_main_cookie_file(MAIN_COOKIE) is None
    assert bili.write_yui_cookie_file(YUI_COOKIE) is None


def test_set_roles_half_swap_end_to_end_reports_cookie_write_failed(monkeypatch, tmp_path):
    """端到端：半交换失败 → COOKIE_WRITE_FAILED，角色表与文件都不变。"""
    bili, _pkg_mod, inst = _prepare_two_accounts(monkeypatch, tmp_path)
    accounts_before = _pkg_mod._read_accounts()
    env_before = (os.environ.get("BILI_COOKIE"), os.environ.get("BILI_YUI_COOKIE"))
    calls = _fail_on_nth_write(bili, monkeypatch, 2)

    out = asyncio.run(inst.bili_set_roles(action="swap"))

    assert out["is_error"] is True
    assert out["error"] == "COOKIE_WRITE_FAILED"
    assert "已按原值回滚" in out["output"]
    assert _pkg_mod._read_accounts() == accounts_before
    assert bili._main_cookie_from_file() == MAIN_COOKIE
    assert bili._yui_cookie_from_file() == YUI_COOKIE
    assert (os.environ.get("BILI_COOKIE"), os.environ.get("BILI_YUI_COOKIE")) == env_before
    assert calls[0] == "yui_cookie.txt" and calls[1] == "main_cookie.txt"
