"""v0.9.48 独立小号校验回归：空残/同号 yui 不得被当成小号。"""

from __future__ import annotations

import asyncio

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


def test_duplicate_yui_cookie_is_not_independent(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    assert inst._yui_cookie_state() == ("", "duplicate")
    assert inst._independent_yui_cookie() == ""
    assert inst._resolve_account("") == "main"


def test_invalid_yui_cookie_is_not_independent(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(
        bili,
        "_cookie",
        lambda role: MAIN_COOKIE if role == "main" else "SESSDATA=yui",
    )
    inst = _inst()

    assert inst._yui_cookie_state() == ("", "invalid")
    assert inst._resolve_account("") == "main"


def test_valid_independent_yui_cookie_is_used(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(
        bili,
        "_cookie",
        lambda role: MAIN_COOKIE if role == "main" else YUI_COOKIE,
    )
    inst = _inst()

    assert inst._yui_cookie_state() == (YUI_COOKIE, "")
    assert inst._resolve_account("") == "yui"
    assert inst._resolve_account("main") == "main"


def test_yui_status_rejects_duplicate_main(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_yui_status())

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_COOKIE"
    assert "与主号是同一个 uid" in out["output"]


def test_enter_live_rejects_duplicate_main_as_yui(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_enter_live(room="12345", account="yui"))

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"


def test_set_roles_single_account_cannot_become_yui(monkeypatch, tmp_path):
    bili, pkg = _bili(), _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "_main_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(bili, "_yui_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    bili.write_main_cookie_file(MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_set_roles(uid=111, role="yui"))

    assert out["is_error"] is True
    assert out["error"] == "NEED_SECOND_ACCOUNT"


def test_set_roles_duplicate_files_are_blocked(monkeypatch, tmp_path):
    bili, pkg = _bili(), _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "_main_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(bili, "_yui_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    bili.write_main_cookie_file(MAIN_COOKIE)
    bili.write_yui_cookie_file(MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_set_roles(action="swap"))

    assert out["is_error"] is True
    assert out["error"] == "DUPLICATE_ACCOUNT"


def test_set_roles_invalid_yui_file_is_blocked(monkeypatch, tmp_path):
    bili, pkg = _bili(), _pkg()
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    monkeypatch.setattr(bili, "_YUI_COOKIE_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "_main_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(bili, "_yui_cookie_file_cache", (0.0, ""))
    monkeypatch.setattr(pkg, "_accounts_path", lambda: str(tmp_path / "accounts.json"))
    bili.write_main_cookie_file(MAIN_COOKIE)
    bili.write_yui_cookie_file("SESSDATA=broken")
    inst = _inst()

    out = asyncio.run(inst.bili_set_roles(action="swap"))

    assert out["is_error"] is True
    assert out["error"] == "INVALID_ACCOUNT"


def test_yui_dynamic_rejects_duplicate_main(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_yui_dynamic(text="test"))

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"


def test_dynamic_delete_rejects_duplicate_main(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_dynamic_delete(dynamic_id="123"))

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"


def test_yui_upload_rejects_duplicate_main(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(inst.bili_yui_upload(file="test.mp4", title="test"))

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"


def test_yui_upload_url_rejects_duplicate_main(monkeypatch):
    bili = _bili()
    monkeypatch.setattr(bili, "_cookie", lambda role: MAIN_COOKIE)
    inst = _inst()

    out = asyncio.run(
        inst.bili_yui_upload_video_url(url="https://example.com/test.mp4", title="test")
    )

    assert out["is_error"] is True
    assert out["error"] == "NO_YUI_ACCOUNT"
