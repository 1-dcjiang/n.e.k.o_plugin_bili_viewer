"""cdp_cookie 接入语义测试：绝不关闭/重启浏览器；静默探测不启动浏览器。

CI（Linux）与本地（Windows）都必须通过：测试显式构造 LOCALAPPDATA 下的
Chrome/Edge 目录，避免平台差异导致分支不同。
"""

from __future__ import annotations


def _cdp():
    from plugin.plugins.bili_viewer import cdp_cookie

    return cdp_cookie


def _setup_browser_dirs(monkeypatch, tmp_path) -> None:
    """模拟 Windows 的 LOCALAPPDATA 与 Chrome/Edge User Data 目录。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "Google" / "Chrome" / "User Data").mkdir(parents=True)
    (tmp_path / "Microsoft" / "Edge" / "User Data").mkdir(parents=True)


def test_ensure_reuses_existing_debug_port(monkeypatch, tmp_path):
    cdp_cookie = _cdp()
    _setup_browser_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: 9333)
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=True)
    assert port == 9333
    assert "已连接" in detail


def test_ensure_never_kills_running_browser(monkeypatch, tmp_path):
    """浏览器普通模式运行且无端口：绝不结束进程，仅给一次性设置引导。"""
    cdp_cookie = _cdp()
    _setup_browser_dirs(monkeypatch, tmp_path)
    killed = []

    def _fake_kill(cmd, **kw):
        killed.append(cmd)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: None)
    monkeypatch.setattr(cdp_cookie, "_browser_running", lambda name: True)
    monkeypatch.setattr(cdp_cookie.subprocess, "run", _fake_kill)
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=True)
    assert port is None
    assert "taskkill" not in detail and killed == []  # 没有发起任何结束进程操作
    assert "首次请先完全退出" in detail  # 提示一次性设置引导


def test_ensure_silent_probe_does_not_launch(monkeypatch, tmp_path):
    """auto_launch=False（插件启动静默探测）：浏览器未运行时不启动任何浏览器。"""
    cdp_cookie = _cdp()
    _setup_browser_dirs(monkeypatch, tmp_path)
    launched = []

    def _fake_launch(name):
        launched.append(name)
        return 9333

    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: None)
    monkeypatch.setattr(cdp_cookie, "_browser_running", lambda name: False)
    monkeypatch.setattr(cdp_cookie, "_launch_debug_browser", _fake_launch)
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=False)
    assert port is None
    assert launched == []  # 静默探测绝不启动浏览器
    assert "静默探测" in detail


def test_ensure_launches_when_user_requests(monkeypatch, tmp_path):
    """用户主动请求接入（auto_launch=True）且浏览器未运行 → 启动调试实例。"""
    cdp_cookie = _cdp()
    _setup_browser_dirs(monkeypatch, tmp_path)
    launched = []
    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: None)
    monkeypatch.setattr(cdp_cookie, "_browser_running", lambda name: False)
    monkeypatch.setattr(cdp_cookie, "_launch_debug_browser", lambda name: (launched.append(name) or 9333))
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=True)
    assert port == 9333
    assert launched == ["Chrome"]
    assert "已启动" in detail
