"""pytest 配置：从插件目录向上定位 N.E.K.O 源码根并注入 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # .../plugins/bili_viewer
_PLUGINS_ROOT = _PLUGIN_DIR.parent  # .../plugins
_NEKO_ROOT = _PLUGINS_ROOT.parent  # N.E.K.O 源码根（含 plugin/ 包）

for _p in (_NEKO_ROOT, _PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(autouse=True)
def _no_background_sync(monkeypatch) -> None:
    """v0.9.32 测试隔离：默认禁用后台实时感知线程。

    BiliViewerPlugin 经 NekoPluginBase 包装后 SdkContext._host_ctx 恒非 None，
    _start_sync_watcher 会真实启动线程；v0.9.31 起线程首轮即执行
    _bridge_reply_tick 扫真实 outbox（存在 done 回执时向 fake_ctx push），
    污染所有断言 pushed 数量的测试。统一设 BILLI_SYNC_DISABLE=1 禁用线程，
    需要验证的 tick（_unread_tick/_bridge_reply_tick）测试均直接调用方法。
    """
    monkeypatch.setenv("BILLI_SYNC_DISABLE", "1")


@pytest.fixture(autouse=True)
def _isolate_bili_runtime_state(monkeypatch) -> None:
    """隔离真实账号 Cookie 与扩展共享缓冲，避免本机运行态污染单元测试。

    - BILI_COOKIE / BILI_YUI_COOKIE 可能来自用户真实环境：不清理时
      "未配置 Cookie 应报 NEED_LOGIN" 会真的发网络请求；
    - web_ui 的弹幕/直播间缓冲由浏览器扩展实时写入：不清理时
      _ext_tab(force=True) 会拿到真实直播间，导致感知兜底测试走错分支。
    测试内部自行 setenv / 写入缓冲的用例不受影响（fixture 在每个用例前执行）。
    """
    monkeypatch.delenv("BILI_COOKIE", raising=False)
    monkeypatch.delenv("BILI_YUI_COOKIE", raising=False)
    try:
        from plugin.plugins.bili_viewer import web_ui

        web_ui._DANMAKU_BUF.clear()
        web_ui._MENTION_NOTIFY.update({"at": 0.0, "text": "", "sender": "", "room": ""})
    except Exception:
        # 收集阶段插件尚未可导入时不影响测试；进入用例后会再次尝试
        pass
