"""desktop_deliver：桌面扩展包投放逻辑测试。

覆盖：目录创建、扩展本体复制、bat 编码（GBK+CRLF）、txt 编码（UTF-8 BOM+CRLF）、
幂等跳过、版本变更重新投放、缺失扩展返回 None。
"""

from __future__ import annotations

import os

# SDK 顶层导入链依赖 utils.plugin_card_fields，测试必须在函数内导入插件模块
# （desktop_deliver 本身是纯 stdlib，无此问题，但包 __init__ 有）


def _make_plugin_dir(tmp_path) -> str:
    plugin_dir = os.path.join(str(tmp_path), "plugin_root")
    ext = os.path.join(plugin_dir, "browser_extension", "bili-bridge")
    os.makedirs(ext, exist_ok=True)
    for f in ("manifest.json", "background.js", "popup.html", "popup.js"):
        with open(os.path.join(ext, f), "w", encoding="utf-8") as fh:
            fh.write("{}")
    with open(os.path.join(plugin_dir, "plugin.toml"), "w", encoding="utf-8") as fh:
        fh.write('version = "9.9.9"\n')
    return plugin_dir


def test_delivers_folder_and_files(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    plugin_dir = _make_plugin_dir(tmp_path)
    desktop = os.path.join(str(tmp_path), "Desktop")
    delivery = desktop_deliver.ensure_desktop_package(plugin_dir, desktop=desktop)
    assert delivery and delivery == os.path.join(desktop, "B站接入桥扩展")
    ext = os.path.join(delivery, "bili-bridge")
    for f in ("manifest.json", "background.js", "popup.html", "popup.js"):
        assert os.path.isfile(os.path.join(ext, f))
    assert os.path.isfile(os.path.join(delivery, "一键安装B站接入桥.bat"))
    assert os.path.isfile(os.path.join(delivery, "使用说明.txt"))
    assert os.path.isfile(os.path.join(delivery, ".delivered_version"))


def test_bat_is_gbk_crlf(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    plugin_dir = _make_plugin_dir(tmp_path)
    delivery = desktop_deliver.ensure_desktop_package(
        plugin_dir, desktop=os.path.join(str(tmp_path), "Desktop")
    )
    bat = os.path.join(delivery, "一键安装B站接入桥.bat")
    data = open(bat, "rb").read()
    # 无 UTF-8 BOM，首字节是 '@'（@echo off）
    assert data[:3] != b"\xef\xbb\xbf"
    assert data[0] == ord("@")
    # 无 LF-only 行（CRLF 编码）
    assert b"\n" not in data.replace(b"\r\n", b"")
    # GBK 可解码且包含关键中文
    text = data.decode("gbk")
    assert "B站接入桥" in text
    assert "@echo off" in text
    assert "chrome://extensions" in text


def test_txt_is_utf8_bom_crlf(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    plugin_dir = _make_plugin_dir(tmp_path)
    delivery = desktop_deliver.ensure_desktop_package(
        plugin_dir, desktop=os.path.join(str(tmp_path), "Desktop")
    )
    txt = os.path.join(delivery, "使用说明.txt")
    data = open(txt, "rb").read()
    assert data[:3] == b"\xef\xbb\xbf"
    assert b"\n" not in data.replace(b"\r\n", b"")
    text = data.decode("utf-8-sig")
    assert "B站接入桥" in text
    assert "一键安装B站接入桥.bat" in text


def test_idempotent_skip_when_version_unchanged(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    plugin_dir = _make_plugin_dir(tmp_path)
    desktop = os.path.join(str(tmp_path), "Desktop")
    delivery = desktop_deliver.ensure_desktop_package(plugin_dir, desktop=desktop)
    bat = os.path.join(delivery, "一键安装B站接入桥.bat")
    mtime = os.path.getmtime(bat)
    # 同一版本再次调用：跳过重建
    delivery2 = desktop_deliver.ensure_desktop_package(plugin_dir, desktop=desktop)
    assert delivery2 == delivery
    assert os.path.getmtime(bat) == mtime


def test_version_change_re_delivers(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    plugin_dir = _make_plugin_dir(tmp_path)
    desktop = os.path.join(str(tmp_path), "Desktop")
    desktop_deliver.ensure_desktop_package(plugin_dir, version="0.6.8", desktop=desktop)
    delivery = desktop_deliver.ensure_desktop_package(plugin_dir, version="0.6.9", desktop=desktop)
    mark = os.path.join(delivery, ".delivered_version")
    assert open(mark, encoding="utf-8").read().strip() == "0.6.9"


def test_missing_extension_returns_none(tmp_path):
    from plugin.plugins.bili_viewer import desktop_deliver

    empty = os.path.join(str(tmp_path), "empty")
    os.makedirs(empty, exist_ok=True)
    assert (
        desktop_deliver.ensure_desktop_package(empty, desktop=os.path.join(str(tmp_path), "Desktop"))
        is None
    )


def test_desktop_dir_probe_returns_existing(tmp_path, monkeypatch):
    from plugin.plugins.bili_viewer import desktop_deliver

    # 注册表探测路径不可测（无 winreg 注入点），验证 fallback 至少返回一个目录
    d = desktop_deliver._desktop_dir()
    assert isinstance(d, str) and d
    # 隔离真实桌面：注入测试桌面后 ensure 应写到注入路径
    plugin_dir = _make_plugin_dir(tmp_path)
    desktop = os.path.join(str(tmp_path), "Desktop2")
    delivery = desktop_deliver.ensure_desktop_package(plugin_dir, desktop=desktop)
    assert delivery and os.path.isdir(os.path.join(desktop, "B站接入桥扩展"))
