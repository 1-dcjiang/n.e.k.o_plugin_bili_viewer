"""cdp_cookie 接入语义测试：绝不关闭/重启浏览器；静默探测不启动浏览器。"""

from __future__ import annotations


def _cdp():
    from plugin.plugins.bili_viewer import cdp_cookie

    return cdp_cookie


def test_ensure_reuses_existing_debug_port(monkeypatch):
    cdp_cookie = _cdp()
    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: 9333)
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=True)
    assert port == 9333
    assert "已连接" in detail


def test_ensure_never_kills_running_browser(monkeypatch):
    """浏览器普通模式运行且无端口：绝不结束进程，仅给一次性设置引导。"""
    cdp_cookie = _cdp()
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


def test_ensure_silent_probe_does_not_launch(monkeypatch):
    """auto_launch=False（插件启动静默探测）：浏览器未运行时不启动任何浏览器。"""
    cdp_cookie = _cdp()
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


def test_ensure_launches_when_user_requests(monkeypatch):
    """用户主动请求接入（auto_launch=True）且浏览器未运行 → 启动调试实例。"""
    cdp_cookie = _cdp()
    launched = []
    monkeypatch.setattr(cdp_cookie, "_find_debug_port", lambda: None)
    monkeypatch.setattr(cdp_cookie, "_browser_running", lambda name: False)
    monkeypatch.setattr(cdp_cookie, "_launch_debug_browser", lambda name: (launched.append(name) or 9333))
    port, detail = cdp_cookie.ensure_debug_browser(auto_launch=True)
    assert port == 9333
    assert launched == ["Chrome"]
    assert "已启动" in detail
