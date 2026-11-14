"""async 阻塞回归：禁止 async 工具直接裸调同步 bili/bili_social 网络函数。

复核 v0.9.36 第 1 项：video_danmaku/live_info/user_info/see_*/send_*/like_all 等
在 async 函数里直接调用同步 urllib，会阻塞事件循环。所有网络调用必须包
`asyncio.to_thread(...)`。这里用 AST 扫描，防止后续新增工具再犯。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent

# 纯函数 / 本地缓存 / 本地文件操作：不发起网络请求，允许直接调用。
_ALLOWED = {
    "BiliError",
    "parse_bili_url",
    "parse_window_title",
    "pick_now_watching",
    "render_see",
    "cookie_uid",
    "redact_text",
    "_fmt_time",
    "_parse_bvid",
    "_cookie",
    "_main_cookie_from_file",
    "_yui_cookie_from_file",
    "write_main_cookie_file",
    "write_yui_cookie_file",
}

_THREAD_FUNCS = {"to_thread", "run_in_executor"}


def _is_network_call(node: ast.Call) -> bool:
    f = node.func
    if not isinstance(f, ast.Attribute) or not isinstance(f.value, ast.Name):
        return False
    if f.value.id not in {"bili", "bili_social"}:
        return False
    name = f.attr
    if name.startswith("format_") or name in _ALLOWED:
        return False
    return True


def _scan_async_offsets(tree: ast.AST) -> list[tuple[str, int, str]]:
    bad: list[tuple[str, int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.fn = ""
            self.depth = 0

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            outer = self.fn
            self.fn = node.name
            self.generic_visit(node)
            self.fn = outer

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _THREAD_FUNCS:
                # to_thread(fn, ...) 的第一个参数就是被包装的调用，跳过整棵子树
                return
            if self.fn and _is_network_call(node):
                bad.append((self.fn, node.lineno, ast.unparse(node)[:100]))
            self.generic_visit(node)

    Visitor().visit(tree)
    return bad


@pytest.mark.parametrize("filename", ["__init__.py", "bili_social.py"])
def test_no_sync_bili_calls_in_async(filename: str) -> None:
    path = _PLUGIN_DIR / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = _scan_async_offsets(tree)
    assert not bad, f"{filename} 的 async 函数里仍有裸调同步 bili.*：\n" + "\n".join(
        f"{fn}:{line}: {src}" for fn, line, src in bad
    )
