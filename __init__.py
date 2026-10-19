"""B站全能助手（bili_viewer）— 让 YUI 浏览、感知并管理哔哩哔哩。

能力：
- 浏览：搜索/视频详情/弹幕/字幕/AI概要/评论/直播信息与实时弹幕/UP主信息
- 感知：读取前台窗口，识别用户在看的应用/站点/页面主题（实时同步给 YUI）
- 管理：查看自己账号与稿件、修改稿件信息、直播时回复弹幕（需 BILI_COOKIE 登录态）

安全边界：读操作只访问公开内容；写操作仅限本人账号内容、需登录态与用户确认；
实时感知仅读取本地窗口标题与进程名，不读取页面内容、不做网络传输。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from plugin.sdk.plugin import NekoPluginBase, lifecycle, llm_tool, neko_plugin, plugin_entry

from . import bili, cdp_cookie, cookie_auto, desktop_deliver, web_ui


def _wrap_err(exc: bili.BiliError) -> dict:
    return {
        "output": exc.message,
        "is_error": True,
        "error": exc.code,
    }


# 实时同步的轮询间隔（秒）与稳定判定次数（连续 N 次相同才推送，防抖动）
_SYNC_POLL_INTERVAL = 2.5
_SYNC_STABLE_COUNT = 2
_SYNC_MIN_INTERVAL = 15.0  # 两次推送的最小间隔（秒），避免频繁切内容时刷屏
_SYNC_DISABLE_ENV = "BILLI_SYNC_DISABLE"
_BROWSER_MONITOR_EVERY = 24  # 浏览器接入监控刷新频率：约每 24 次轮询（≈60s）一次

# 插件配置段与 Cookie 配置键
_CONFIG_SECTION = "settings"
_CONFIG_COOKIE_KEY = "bili_cookie"
_CONFIG_ALLOW_SCAN_KEY = "allow_cookie_scan"

_COOKIE_SOURCE_MANUAL = "manual"
_COOKIE_SOURCE_AUTO = "auto"
_COOKIE_SOURCE_EXTENSION = "extension"
_COOKIE_SOURCE_NONE = "none"


def _apply_cookie_from_config(cfg: dict, env: dict) -> str:
    """决定实际生效的 BILI_COOKIE：环境变量优先，其次插件配置（settings.bili_cookie）。

    返回最终生效的 Cookie 字符串（可能为空）。纯函数便于测试。
    """
    env_cookie = str(env.get("BILI_COOKIE", "") or "").strip()
    if env_cookie:
        return env_cookie
    section = cfg.get(_CONFIG_SECTION) if isinstance(cfg, dict) else None
    if isinstance(section, dict):
        val = section.get(_CONFIG_COOKIE_KEY)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _allow_cookie_scan(cfg: dict) -> bool:
    """用户是否已授权自动读取浏览器 Cookie（settings.allow_cookie_scan）。"""
    section = cfg.get(_CONFIG_SECTION) if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return False
    val = section.get(_CONFIG_ALLOW_SCAN_KEY)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return False


def _cookie_summary(cookie: str) -> str:
    """简短描述 Cookie 是否含登录态（不泄露内容）。"""
    if not cookie:
        return "未配置"
    marks = []
    if "SESSDATA=" in cookie:
        marks.append("SESSDATA（登录态）")
    if "bili_jct=" in cookie:
        marks.append("bili_jct（写操作凭证）")
    if not marks:
        return "已配置（无登录态字段）"
    return "含 " + "、".join(marks)


def _cookie_uid(cookie: str) -> str:
    """从 Cookie 提取 DedeUserID（账号 uid），无则返回空串。纯函数便于测试。"""
    if not cookie:
        return ""
    m = re.search(r"(?:^|;\s*)DedeUserID=(\d+)", cookie)
    return m.group(1) if m else ""


def _sync_text(parsed: dict) -> str | None:
    """把识别结果渲染为注入 YUI 上下文的一句话。"""
    kind = parsed.get("kind")
    if kind == "unknown":
        return None
    if parsed.get("is_bili"):
        label = "直播" if kind == "live" else "视频"
        subject = parsed.get("subject") or "未知内容"
        # 附带精确标识：直播给房间号（YUI 可直接发弹幕/查详情）、视频给 BV 号。
        # 明确指示这些编号仅供内部操作（发弹幕/查详情），不要向用户复述出来。
        extra = ""
        if kind == "live" and parsed.get("room_id"):
            extra = f"，房间号 {parsed['room_id']}（该编号仅供发弹幕等内部操作，请勿向用户复述）"
        elif kind == "video" and parsed.get("bvid"):
            extra = f"，BV号 {parsed['bvid']}（该编号仅供发弹幕等内部操作，请勿向用户复述）"
        return f"（实时感知：用户当前正在看 B站{label}「{subject}」{extra}）"
    if parsed.get("site"):
        return f"（实时感知：用户当前正在用 {parsed['site']}，页面「{parsed.get('subject') or '未知'}」）"
    if kind == "web":
        return f"（实时感知：用户当前正在浏览网页「{parsed.get('subject') or '未知'}」）"
    return f"（实时感知：用户当前正在使用应用 {parsed.get('app') or '未知'}，窗口「{parsed.get('subject') or '未知'}」）"


def _window_fingerprint(parsed: dict) -> str:
    """内容指纹：app|site|kind|subject。"""
    return "|".join(
        (
            str(parsed.get("app") or ""),
            str(parsed.get("site") or ""),
            str(parsed.get("kind") or ""),
            str(parsed.get("subject") or ""),
        )
    )


@neko_plugin
class BiliViewerPlugin(NekoPluginBase):
    """N.E.K.O. 插件：哔哩哔哩内容浏览与实时感知助手。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._sync_thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        # 启动后台实时感知线程（守护线程，不阻塞插件生命周期）
        self._start_sync_watcher()

    # ══════════ 实时感知同步（后台线程） ══════════

    def _start_sync_watcher(self) -> None:
        """启动后台轮询线程；仅在宿主上下文有效（非测试环境）且未禁用时启动。"""
        if os.environ.get(_SYNC_DISABLE_ENV, "").strip() not in ("", "0"):
            return
        ctx = getattr(self, "ctx", None)
        if ctx is None or not callable(getattr(ctx, "push_message", None)):
            return  # 无宿主上下文
        host = getattr(ctx, "_host_ctx", None)
        if host is None:
            return  # 测试/空宿主包装（如 SdkContext(None)）不启动线程
        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return  # 防重复启动
            self._sync_thread = threading.Thread(
                target=self._sync_watcher_loop,
                name="bili-now-watching-sync",
                daemon=True,
            )
            self._sync_thread.start()

    def _sync_watcher_loop(self) -> None:
        """轮询窗口，内容变化且稳定后向 YUI 推送（B站内容主动触发，其余静默注入）。"""
        last_pushed: str | None = None
        candidate: str | None = None
        candidate_count = 0
        last_push_ts = 0.0
        monitor_ticks = 0
        while True:
            # 浏览器接入低频监控：约每 _BROWSER_MONITOR_EVERY 次轮询刷新一次
            monitor_ticks += 1
            if monitor_ticks >= _BROWSER_MONITOR_EVERY:
                monitor_ticks = 0
                try:
                    self._browser_monitor = self._monitor_browser()
                except Exception:
                    pass
            try:
                tab = self._ext_tab()
                if tab:
                    # 扩展标签页：本地 URL 解析（零网络），精确感知当前 B站页面
                    parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
                    if parsed is None:
                        parsed = bili.parse_window_title(tab.get("title", ""), "chrome.exe")
                else:
                    parsed = bili.pick_now_watching()
            except Exception:
                # 感知单次失败不退出线程：等待后重试，避免同步永久失效
                threading.Event().wait(_SYNC_POLL_INTERVAL)
                continue
            if not parsed:
                # 无可感知窗口（如仅剩聊天客户端/终端）：跳过，保持上一次有效感知
                threading.Event().wait(_SYNC_POLL_INTERVAL)
                continue
            fingerprint = _window_fingerprint(parsed)
            if fingerprint != candidate:
                candidate, candidate_count = fingerprint, 0
            else:
                candidate_count += 1
            now = time.time()
            if (
                candidate_count >= _SYNC_STABLE_COUNT
                and fingerprint != last_pushed
                and fingerprint
                and now - last_push_ts >= _SYNC_MIN_INTERVAL
            ):
                text = _sync_text(parsed)
                if text:
                    # B站内容主动触发（respond，YUI 自动跟上）；其余静默注入（read）
                    behavior = "respond" if parsed.get("is_bili") else "read"
                    try:
                        self.ctx.push_message(
                            visibility=[],
                            ai_behavior=behavior,
                            parts=[{"type": "text", "text": text}],
                            source=self.plugin_id,
                            coalesce_key="now_watching_sync",
                            priority=0,
                        )
                        last_pushed = fingerprint
                        last_push_ts = now
                    except Exception:
                        pass  # 推送失败不中断轮询
            threading.Event().wait(_SYNC_POLL_INTERVAL)

    # ══════════ 生命周期 ══════════

    def _run_bind(self, auto_launch: bool) -> dict:
        """执行一次浏览器接入绑定（同步），返回面板可读的结果字典。

        顺序：CDP 运行时接入（浏览器保持运行）→ 仅当 CDP 明确“浏览器完全未运行”
        时做文件路径兜底（浏览器运行中 Cookies 库被锁，直接跳过）。
        auto_launch=True（用户主动/面板点击）时允许启动调试实例；False（启动静默
        探测）时绝不启动浏览器。任何失败都静默降级，bili_bind_status 会现场重探。
        """
        result: dict = {"ok": False, "detail": "未接入"}
        try:
            _status, auto_cookie, _detail = cdp_cookie.bind_via_cdp(auto_launch=auto_launch)
        except Exception:
            _status, auto_cookie, _detail = cdp_cookie._STATUS_FAIL, "", "CDP 接入异常"
        result["detail"] = _detail or "未接入"
        if auto_cookie and "SESSDATA=" in auto_cookie:
            os.environ["BILI_COOKIE"] = auto_cookie
            self._cookie_source = _COOKIE_SOURCE_AUTO
            result.update({"ok": True, "bound": True, "detail": f"已自动接入 B站登录态（{_detail}）"})
            return result
        if _status == cdp_cookie._STATUS_NO_BROWSER:
            try:
                _fstatus, _fcookie, _fd = cookie_auto.detect_bili_cookie()
                if (
                    _fstatus == cookie_auto._STATUS_BOUND
                    and _fcookie
                    and "SESSDATA=" in _fcookie
                ):
                    os.environ["BILI_COOKIE"] = _fcookie
                    self._cookie_source = _COOKIE_SOURCE_AUTO
                    result.update({"ok": True, "bound": True, "detail": f"已从浏览器数据自动读取 B站登录态（{_fd}）"})
                    return result
            except Exception:
                pass  # 文件兜底失败不致命
        return result

    def _bg_bind(self) -> None:
        """后台异步绑定：startup 绝不等待网络，绑定完成后再写入登录态。"""

        def _run() -> None:
            try:
                self._run_bind(auto_launch=False)
            except Exception:
                pass  # 后台绑定失败不影响插件

        threading.Thread(target=_run, name="bili-bg-bind", daemon=True).start()

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        # 0) 后台投放「B站接入桥」扩展包到桌面（纯本地文件操作，毫秒级，失败静默）
        try:
            _plugin_dir = os.path.dirname(os.path.abspath(__file__))
            desktop_deliver.ensure_desktop_package_async(_plugin_dir)
        except Exception:
            pass
        # 0.5) 常驻本地接入面板（127.0.0.1:8765）：扩展一装好就能直连推送，
        #      不再需要先调用 bili_bind_web 才启动面板。端口被占（已有实例）则复用/跳过。
        try:
            self._ensure_panel()
        except Exception:
            pass
        # 1) 手动配置（settings.bili_cookie / 环境变量 BILI_COOKIE）——2s 预算，快速返回
        try:
            cfg = await self.config.dump(timeout=2.0)
            cfg = cfg if isinstance(cfg, dict) else {}
        except Exception:
            cfg = {}
        cookie = _apply_cookie_from_config(cfg, os.environ)
        source = _COOKIE_SOURCE_MANUAL if cookie else _COOKIE_SOURCE_NONE
        # 2) 未手动配置且用户已授权 → 后台异步自动接入（startup 立即返回，绝不等待网络）
        if not cookie and _allow_cookie_scan(cfg):
            self._bg_bind()
        if cookie:
            os.environ["BILI_COOKIE"] = cookie
        self._cookie_source = source
        self._start_sync_watcher()
        return {"status": "ready", "plugin": "bili_viewer"}

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        # 关闭常驻面板，释放 8765 端口（线程化避免阻塞关闭流程）
        try:
            panel = getattr(self, "_bind_panel", None)
            if panel is not None:
                await asyncio.to_thread(panel.stop)
        except Exception:
            pass
        return {"status": "shutdown"}

    # ══════════ 搜索 ══════════

    @llm_tool(
        name="bili_search",
        description=(
            "搜索哔哩哔哩内容。何时用：用户说'搜一下B站/找找视频/查主播/看直播'时。"
            "参数 keyword：搜索词；type：video（视频，默认）/user（用户）/live（直播）。"
            "返回候选列表（视频含 BV 号，可直接配合 bili_video_info 查看）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "type": {"type": "string", "enum": ["video", "user", "live"], "description": "搜索类型"},
            },
            "required": ["keyword"],
        },
        timeout=25.0,
    )
    async def bili_search(self, *, keyword: str = "", type: str = "video", **_):
        params = _.get("parameters") or {}
        keyword = (keyword or params.get("keyword") or "").strip()
        stype = (type or params.get("type") or "video").strip().lower()
        try:
            results = bili.search(keyword, stype)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        lines = []
        for i, r in enumerate(results, 1):
            if r["type"] == "video":
                lines.append(f"{i}. {r['title']}（{r['author']}，播放 {r['play']}，时长 {r['duration']}）BV号 {r['bvid']}")
            elif r["type"] == "user":
                lines.append(f"{i}. {r['uname']}（粉丝 {r['fans']}，视频 {r['videos']}）mid {r['mid']}")
            else:
                status = "直播中" if r["live_status"] == 1 else "未开播"
                lines.append(f"{i}. {r['title']}（{r['uname']}，{status}，在线 {r['online']}）房间 {r['roomid']}")
        return {"output": f"「{keyword}」搜索结果（{stype}）：\n" + "\n".join(lines), "is_error": False, "results": results}

    # ══════════ 视频 ══════════

    @llm_tool(
        name="bili_video_info",
        description=(
            "查看哔哩哔哩视频详情。何时用：用户说'看看这个视频/这视频什么内容'或给出 BV 号/链接时。"
            "参数 bvid：BV 号或完整视频链接。返回标题、UP主、时长、分区、播放/弹幕/点赞/投币/收藏、"
            "分P信息、字幕可用性与标签。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV 号或视频链接"},
            },
            "required": ["bvid"],
        },
        timeout=20.0,
    )
    async def bili_video_info(self, *, bvid: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        if not bvid:
            return {"output": "请提供视频的 BV 号或链接。", "is_error": True, "error": "NO_BVID"}
        try:
            info = bili.video_info(bvid)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_video_info(info), "is_error": False, "video": info}

    @llm_tool(
        name="bili_video_danmaku",
        description=(
            "查看视频弹幕（文本）。何时用：用户说'看看弹幕在说什么/弹幕都在刷什么'时。"
            "参数 bvid：BV 号或链接；limit：返回条数（默认 60，最大 300）。默认取第一P。"
            "返回带时间点的弹幕文本列表，可了解观众反应。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV 号或视频链接"},
                "limit": {"type": "integer", "description": "弹幕条数上限，默认 60"},
            },
            "required": ["bvid"],
        },
        timeout=25.0,
    )
    async def bili_video_danmaku(self, *, bvid: str = "", limit: int = 0, **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        try:
            n = int(limit or params.get("limit") or 60)
        except (TypeError, ValueError):
            n = 60
        n = max(1, min(n, 300))
        if not bvid:
            return {"output": "请提供视频的 BV 号或链接。", "is_error": True, "error": "NO_BVID"}
        try:
            danmakus = bili.video_danmaku(bvid, limit=n)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"弹幕（前 {len(danmakus)} 条）：\n" + "\n".join(danmakus), "is_error": False, "danmakus": danmakus}

    @llm_tool(
        name="bili_video_subtitle",
        description=(
            "获取视频 CC 字幕文本（若有）。何时用：需要了解视频讲了什么、做内容理解时。"
            "参数 bvid：BV 号或链接；lan：字幕语言（默认 zh-CN）。返回逐句字幕文本，适合快速通读视频内容。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV 号或视频链接"},
                "lan": {"type": "string", "description": "字幕语言代码，默认 zh-CN"},
            },
            "required": ["bvid"],
        },
        timeout=25.0,
    )
    async def bili_video_subtitle(self, *, bvid: str = "", lan: str = "zh-CN", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        lan = (lan or params.get("lan") or "zh-CN").strip()
        if not bvid:
            return {"output": "请提供视频的 BV 号或链接。", "is_error": True, "error": "NO_BVID"}
        try:
            text = bili.video_subtitle(bvid, lan=lan)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"视频字幕（{lan}）：\n" + text[:3000], "is_error": False, "subtitle": text[:6000]}

    @llm_tool(
        name="bili_video_ai_summary",
        description=(
            "获取 B站官方 AI 视频概要。何时用：用户说'这个视频讲的什么/概括一下/没时间看'时。"
            "参数 bvid：BV 号或链接。仅部分视频提供 AI 概要，无则提示。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV 号或视频链接"},
            },
            "required": ["bvid"],
        },
        timeout=20.0,
    )
    async def bili_video_ai_summary(self, *, bvid: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        if not bvid:
            return {"output": "请提供视频的 BV 号或链接。", "is_error": True, "error": "NO_BVID"}
        try:
            summary = bili.video_ai_summary(bvid)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"AI 视频概要：\n{summary}", "is_error": False, "summary": summary}

    @llm_tool(
        name="bili_video_comments",
        description=(
            "查看视频评论。何时用：用户说'看看评论/大家都在说什么/热评'时。"
            "参数 bvid：BV 号或链接；mode：hot（热门，默认）/new（最新）；page：页码；limit：条数（默认 12）。"
            "返回用户、内容、点赞数与回复数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV 号或视频链接"},
                "mode": {"type": "string", "enum": ["hot", "new"], "description": "hot 热门 / new 最新"},
                "page": {"type": "integer", "description": "页码，默认 1"},
                "limit": {"type": "integer", "description": "条数，默认 12"},
            },
            "required": ["bvid"],
        },
        timeout=25.0,
    )
    async def bili_video_comments(self, *, bvid: str = "", mode: str = "hot", page: int = 1, limit: int = 0, **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        mode = (mode or params.get("mode") or "hot").strip().lower()
        mode = "new" if mode == "new" else "hot"
        try:
            page = int(page if page else params.get("page") or 1)
            n = int(limit if limit else params.get("limit") or 12)
        except (TypeError, ValueError):
            page, n = 1, 12
        n = max(1, min(n, 30))
        page = max(1, page)
        if not bvid:
            return {"output": "请提供视频的 BV 号或链接。", "is_error": True, "error": "NO_BVID"}
        try:
            comments = bili.video_comments(bvid, mode=mode, page=page, limit=n)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        lines = []
        for i, c in enumerate(comments, 1):
            when = bili._fmt_time(c["ctime"])
            lines.append(f"{i}. {c['user']}（{when}，赞 {c['like']}，回复 {c['rcount']}）：\n   {c['content'][:200]}")
        return {"output": f"视频评论（{mode}，第 {page} 页）：\n" + "\n".join(lines), "is_error": False, "comments": comments}

    # ══════════ 直播 ══════════

    @llm_tool(
        name="bili_live_info",
        description=(
            "查看 B站直播间信息。何时用：用户说'看看这个直播间/直播开着吗/在线多少人'或给出房间号/链接时。"
            "参数 room：房间号、直播间链接或直播搜索到的房间号。"
            "返回标题、主播、状态、在线人数、分区与简介。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接（如 live.bilibili.com/12345）"},
            },
            "required": ["room"],
        },
        timeout=20.0,
    )
    async def bili_live_info(self, *, room: str = "", **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        if not room:
            return {"output": "请提供直播房间号或直播间链接。", "is_error": True, "error": "NO_ROOM"}
        try:
            info = bili.live_info(room)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_live_info(info), "is_error": False, "live": info}

    @llm_tool(
        name="bili_live_danmaku",
        description=(
            "采集 B站直播间的实时弹幕。何时用：用户说'看看直播间弹幕/现在弹幕在刷什么'时。"
            "参数 room：房间号或链接；duration：采集秒数（默认 8，最大 30）。"
            "连接弹幕服务器匿名采集数秒，返回弹幕文本与发送者。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接"},
                "duration": {"type": "integer", "description": "采集秒数，默认 8"},
            },
            "required": ["room"],
        },
        timeout=40.0,
    )
    async def bili_live_danmaku(self, *, room: str = "", duration: int = 0, **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        try:
            duration = int(duration if duration else params.get("duration") or 8)
        except (TypeError, ValueError):
            duration = 8
        duration = max(1, min(duration, 30))  # 与 bili.live_danmaku 内部上限一致
        if not room:
            return {"output": "请提供直播房间号或直播间链接。", "is_error": True, "error": "NO_ROOM"}
        try:
            items = bili.live_danmaku(room, duration=duration)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        lines = [f"{i}. {d['user']}：{d['text']}" for i, d in enumerate(items, 1)]
        return {"output": f"直播间实时弹幕（采集 {duration} 秒，{len(items)} 条）：\n" + "\n".join(lines), "is_error": False, "danmakus": items}

    # ══════════ UP主 ══════════

    @llm_tool(
        name="bili_user_info",
        description=(
            "查看 B站用户（UP主）信息。何时用：用户说'查一下这个UP主/他有多少粉丝'时。"
            "参数 mid：用户数字 ID；或提供用户名时先用 bili_search(type=user) 找到 mid。"
            "返回昵称、粉丝、关注、视频数、等级与签名。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mid": {"type": "integer", "description": "B站用户数字 ID（mid）"},
            },
            "required": ["mid"],
        },
        timeout=20.0,
    )
    async def bili_user_info(self, *, mid: int = 0, **_):
        params = _.get("parameters") or {}
        try:
            mid = int(mid or params.get("mid") or 0)
        except (TypeError, ValueError):
            mid = 0
        if not mid or mid <= 0:
            return {"output": "请提供 UP主的数字 mid（可用 bili_search(type=user) 先搜索）。", "is_error": True, "error": "NO_MID"}
        try:
            info = bili.user_info(mid)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_user_info(info), "is_error": False, "user": info}

    @llm_tool(
        name="bili_user_videos",
        description=(
            "查看 UP主的投稿视频列表（按发布时间倒序）。何时用：用户说'这个UP主发过什么视频/最近投稿'时。"
            "参数 mid：用户数字 ID；page：页码；limit：条数（默认 15）。"
            "返回视频标题、播放量、时长与 BV 号。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mid": {"type": "integer", "description": "B站用户数字 ID（mid）"},
                "page": {"type": "integer", "description": "页码，默认 1"},
                "limit": {"type": "integer", "description": "条数，默认 15"},
            },
            "required": ["mid"],
        },
        timeout=25.0,
    )
    async def bili_user_videos(self, *, mid: int = 0, page: int = 1, limit: int = 0, **_):
        params = _.get("parameters") or {}
        try:
            mid = int(mid or params.get("mid") or 0)
            page = int(page if page else params.get("page") or 1)
            n = int(limit if limit else params.get("limit") or 15)
        except (TypeError, ValueError):
            mid, page, n = 0, 1, 15
        n = max(1, min(n, 30))
        page = max(1, page)
        if not mid or mid <= 0:
            return {"output": "请提供 UP主的数字 mid（可用 bili_search(type=user) 先搜索）。", "is_error": True, "error": "NO_MID"}
        try:
            videos = bili.user_videos(mid, page=page, limit=n)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        lines = []
        for i, v in enumerate(videos, 1):
            lines.append(f"{i}. {v['title']}（播放 {v['play']}，时长 {v['duration']}）BV号 {v['bvid']}")
        return {"output": f"UP主 mid {mid} 的投稿（第 {page} 页）：\n" + "\n".join(lines), "is_error": False, "videos": videos}

    # ══════════ 我的账号 / 稿件（需 BILI_COOKIE 登录态） ══════════

    @llm_tool(
        name="bili_now_watching",
        description=(
            "感知用户当前正在看/用的内容（读取前台窗口标题与进程，通用感知，不限于 B站）。何时用：用户说"
            "'我现在在看XX/我切视频了/跟着我看/看看我现在在干嘛/我正开着什么'时。"
            "返回 app（进程）、window（窗口标题）、site（站点：bilibili/zhihu/weibo/youtube/steam 等）、"
            "subject（页面/窗口主题）、kind（video/live/web/app）；若在 B站视频/直播页会自动匹配视频详情"
            "（标题/UP主/播放/弹幕/BV号/简介）。注意：BV号/房间号等编号仅供发弹幕/查详情等内部操作使用，"
            "回复用户时无需复述编号本身。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        timeout=25.0,
    )
    async def bili_now_watching(self, **_):
        try:
            tab = self._ext_tab()
            info = bili.now_watching(tab=tab)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_now_watching(info), "is_error": False, "watching": info}

    @llm_tool(
        name="bili_my_info",
        description=(
            "查看当前 B站账号（BILI_COOKIE）信息。何时用：用户说'看看我的B站账号/我多少粉丝/我是大会员吗'时。"
            "返回昵称、uid、等级、硬币、大会员状态、关注与粉丝数。需要配置 BILI_COOKIE 环境变量。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        timeout=20.0,
    )
    async def bili_my_info(self, **_):
        try:
            info = bili.my_info()
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_my_info(info), "is_error": False, "account": info}

    @llm_tool(
        name="bili_my_videos",
        description=(
            "查看当前 B站账号（BILI_COOKIE）的投稿稿件列表。何时用：用户说'看看我自己的稿件/我的投稿'时。"
            "参数 page：页码；limit：条数（默认 10）。返回标题、状态（公开/审核中）、播放量、时长与 BV 号。"
            "需要配置 BILI_COOKIE 环境变量。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "页码，默认 1"},
                "limit": {"type": "integer", "description": "条数，默认 10"},
            },
            "required": [],
        },
        timeout=25.0,
    )
    async def bili_my_videos(self, *, page: int = 1, limit: int = 0, **_):
        params = _.get("parameters") or {}
        try:
            page = int(page if page else params.get("page") or 1)
            n = int(limit if limit else params.get("limit") or 10)
        except (TypeError, ValueError):
            page, n = 1, 10
        n = max(1, min(n, 30))
        page = max(1, page)
        try:
            videos = bili.my_videos(page=page, limit=n)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"我的稿件（第 {page} 页）：\n" + bili.format_my_videos(videos), "is_error": False, "videos": videos}

    @llm_tool(
        name="bili_edit_video",
        description=(
            "修改自己 B站稿件的信息（标题/简介/标签）。何时用：用户明确要求'改我的视频标题/简介/标签'时。"
            "这是写操作：只能修改当前账号（BILI_COOKIE）自己的稿件，服务端校验所有权。"
            "参数 bvid：要修改的 BV 号；title/desc/tag：要更新的内容（不传则保留原值）。"
            "需要配置 BILI_COOKIE（含 bili_jct）环境变量，且仅在用户明确确认后调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "要修改的稿件 BV 号"},
                "title": {"type": "string", "description": "新标题（可选）"},
                "desc": {"type": "string", "description": "新简介（可选）"},
                "tag": {"type": "string", "description": "新标签，逗号分隔（可选）"},
            },
            "required": ["bvid"],
        },
        timeout=30.0,
    )
    async def bili_edit_video(self, *, bvid: str = "", title: str = "", desc: str = "", tag: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        if not bvid:
            return {"output": "请提供要修改的稿件 BV 号。", "is_error": True, "error": "NO_BVID"}
        try:
            result = bili.edit_video(
                bvid,
                title=(title or params.get("title") or None),
                desc=(desc or params.get("desc") or None),
                tag=(tag or params.get("tag") or None),
            )
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {
            "output": f"稿件已更新：{result['title']}\n简介：{result['desc'][:100] or '无'}\n标签：{result['tag'] or '无'}",
            "is_error": False,
            "result": result,
        }

    @llm_tool(
        name="bili_live_send_danmaku",
        description=(
            "向 B站直播间发送一条弹幕。何时用：直播时用户要求'回复弹幕/发一条弹幕'时，"
            "配合 bili_live_danmaku 采集实时弹幕、理解后再回复。"
            "参数 room：房间号或链接；text：弹幕内容（最长 30 字）。"
            "这是写操作：需要配置 BILI_COOKIE（含 bili_jct 与 SESSDATA）环境变量，"
            "且仅在用户明确确认后调用。发送后回复用户时无需复述房间号。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接"},
                "text": {"type": "string", "description": "要发送的弹幕内容，最长 30 字"},
            },
            "required": ["room", "text"],
        },
        timeout=30.0,
    )
    async def bili_live_send_danmaku(self, *, room: str = "", text: str = "", **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        text = (text or params.get("text") or "").strip()
        if not room:
            return {"output": "请提供直播房间号或链接。", "is_error": True, "error": "NO_ROOM"}
        if not text:
            return {"output": "请提供要发送的弹幕内容。", "is_error": True, "error": "EMPTY_MSG"}
        try:
            result = bili.live_send_danmaku(room, text)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"已向直播间 {result['room_id']} 发送弹幕：{result['text']}", "is_error": False, "result": result}

    @llm_tool(
        name="bili_video_send_danmaku",
        description=(
            "向 B站视频发送一条弹幕（投到视频时间轴）。何时用：用户在视频页要求'给这个视频发弹幕/"
            "发一条弹幕'时，配合实时感知推送的 BV 号直接使用。"
            "参数 bvid：视频 BV 号；text：弹幕内容（最长 100 字）；"
            "progress：弹幕出现的时间点（秒，可选，默认 0 即视频开头）。"
            "这是写操作：需要配置 BILI_COOKIE（含 bili_jct 与 SESSDATA）环境变量，"
            "且仅在用户明确确认后调用。发送后回复用户时无需复述 BV 号。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "视频 BV 号"},
                "text": {"type": "string", "description": "要发送的弹幕内容，最长 100 字"},
                "progress": {"type": "integer", "description": "弹幕出现的时间点（秒），默认 0"},
            },
            "required": ["bvid", "text"],
        },
        timeout=30.0,
    )
    async def bili_video_send_danmaku(self, *, bvid: str = "", text: str = "", progress: int = 0, **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        text = (text or params.get("text") or "").strip()
        try:
            progress = int(progress if progress else params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if not bvid:
            return {"output": "请提供视频 BV 号。", "is_error": True, "error": "NO_BVID"}
        if not text:
            return {"output": "请提供要发送的弹幕内容。", "is_error": True, "error": "EMPTY_MSG"}
        try:
            result = bili.send_video_danmaku(bvid, text, progress_ms=progress * 1000)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {
            "output": f"已向视频 {result['bvid']} 发送弹幕：{result['text']}（时间点 {result['progress_ms'] // 1000} 秒）",
            "is_error": False,
            "result": result,
        }

    # ══════════ 自动路由入口（plugin_entry：TaskExecutor 评估可见） ══════════
    # llm_tool 走 LLM 函数调用通道；plugin_entry 走 TaskExecutor 的 UserPlugin
    # 路由通道（评估时 LLM 依据插件 entries 判断"哪个插件能执行该请求"并路由）。
    # 此前 bili_viewer 只有 llm_tool、无任何 plugin_entry，导致评估判定
    # "可用插件列表中无支持发送弹幕的插件" → YUI 永远无法路由发弹幕。这里注册
    # run（通用兜底）与 bili_danmaku（弹幕专用）两个入口修复该问题。

    @plugin_entry(
        id="run",
        name="B站全能助手",
        description=(
            "B站全能助手（bili_viewer）：感知用户当前正在看的B站直播/视频"
            "（直播间房间号/视频BV号，编号仅供内部操作，回复用户时请勿复述）；"
            "向B站直播间或视频发送弹幕；查看视频详情/弹幕/字幕/AI概要/评论、"
            "直播间信息与实时弹幕、UP主信息与投稿；搜索视频/直播/用户；"
            "查看和修改自己的稿件；查询B站账号绑定状态。"
            "用户请求'发弹幕''看视频详情''搜B站''看直播''查UP主''发一条弹幕'等B站相关操作时使用本插件。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作类型：send_live_danmaku（发直播弹幕）/ send_video_danmaku（发视频弹幕）/ query（查询与感知）",
                },                "room": {"type": "string", "description": "直播间房间号或链接（发直播弹幕时必填）"},
                "bvid": {"type": "string", "description": "视频BV号（发视频弹幕时必填）"},
                "text": {"type": "string", "description": "弹幕内容，直播最长 30 字、视频最长 100 字"},
                "progress": {"type": "integer", "description": "视频弹幕出现的时间点（秒），默认 0 即开头"},
            },
            "required": ["action"],
        },
        timeout=60.0,
    )
    async def _entry_run(self, **kwargs):
        params = kwargs.get("parameters") or {}
        action = kwargs.get("action") or params.get("action") or ""
        room = str(kwargs.get("room") or params.get("room") or "")
        bvid = str(kwargs.get("bvid") or params.get("bvid") or "")
        text = str(kwargs.get("text") or params.get("text") or "")
        try:
            progress = int(kwargs.get("progress") or params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if action in ("send_live_danmaku", "live_danmaku"):
            return await self.bili_live_send_danmaku(room=room, text=text)
        if action in ("send_video_danmaku", "video_danmaku"):
            return await self.bili_video_send_danmaku(bvid=bvid, text=text, progress=progress)
        # 兜底：感知当前正在看的内容（若用户请求是 B站相关但未给具体参数）
        tab = self._ext_tab()
        if tab:
            parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
            if parsed is not None:
                return {
                    "output": f"当前正在看：{tab.get('title', '')}（{parsed['type']}）",
                    "is_error": False,
                    "result": parsed,
                }
        return {
            "output": "请指定操作：发弹幕（action=send_live_danmaku/send_video_danmaku，需 room/bvid 与 text），或查询B站内容。",
            "is_error": True,
            "error": "NO_ACTION",
        }

    @plugin_entry(
        id="bili_danmaku",
        name="发送B站弹幕",
        description=(
            "向B站直播间或视频发送弹幕。用户说'发弹幕''发一条弹幕''帮我发弹幕'时使用："
            "直播弹幕需房间号（room），视频弹幕需BV号（bvid），内容为 text；"
            "发送后回复用户时无需复述房间号/BV号。需要已绑定B站账号。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接（发直播弹幕时必填）"},
                "bvid": {"type": "string", "description": "视频BV号（发视频弹幕时必填）"},
                "text": {"type": "string", "description": "弹幕内容，直播最长 30 字、视频最长 100 字"},
                "progress": {"type": "integer", "description": "视频弹幕出现的时间点（秒），默认 0 即开头"},
            },
            "required": ["text"],
        },
        timeout=30.0,
    )
    async def _entry_danmaku(self, **kwargs):
        params = kwargs.get("parameters") or {}
        room = str(kwargs.get("room") or params.get("room") or "")
        bvid = str(kwargs.get("bvid") or params.get("bvid") or "")
        text = str(kwargs.get("text") or params.get("text") or "")
        try:
            progress = int(kwargs.get("progress") or params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if bvid:
            return await self.bili_video_send_danmaku(bvid=bvid, text=text, progress=progress)
        if room:
            return await self.bili_live_send_danmaku(room=room, text=text)
        return {"output": "请提供直播间房间号（room）或视频BV号（bvid）与弹幕内容（text）。", "is_error": True, "error": "NO_TARGET"}

    # ══════════ 账号绑定（CDP 运行时接入 + 监控） ══════════

    def _monitor_browser(self) -> dict:
        """低频浏览器接入监控：探测调试端口存活与 B站登录态。

        供 bili_bind_status 报告，也供 watcher 周期刷新 self._browser_monitor。
        返回 {connected, detail, at}。
        """
        try:
            status, _cookie, detail = cdp_cookie.bind_via_cdp(auto_launch=False)
            connected = status == cdp_cookie._STATUS_BOUND or bool(_cookie and "SESSDATA=" in _cookie)
            return {"connected": connected, "detail": detail, "at": time.time()}
        except Exception as exc:
            return {"connected": False, "detail": f"浏览器接入监控异常：{str(exc)[:60]}", "at": time.time()}

    # ── 网页接入面板回调（由 web_ui.BindPanel 注入调用，运行在面板线程） ──

    def _ext_last_seen(self) -> float:
        return float(getattr(self, "_ext_last_seen_ts", 0) or 0)

    def _receive_ext_cookie(self, cookie: str, at: int) -> dict:
        """接收浏览器扩展（B站接入桥）推送的登录态：写入环境变量并标记来源。

        仅接受 127.0.0.1 本机来源（面板服务本身只监听本机）；cookie 不上传不写日志。
        扩展推送的 at 为空（首次握手）时用当前时间。
        """
        ts = float(at) if at and at > 0 else time.time()
        self._ext_last_seen_ts = ts
        cookie = str(cookie or "").strip()
        if "SESSDATA=" in cookie:
            os.environ["BILI_COOKIE"] = cookie
            self._cookie_source = _COOKIE_SOURCE_EXTENSION
            return {"ok": True, "bound": True, "detail": "已通过浏览器扩展接入 B站登录态"}
        return {"ok": True, "bound": False, "detail": "扩展已推送，但浏览器中没有有效 B站登录态"}

    def _receive_ext_tab(self, url: str, title: str, at: int) -> dict:
        """接收浏览器扩展推送的当前活动标签页（URL + 标题），供精确感知。

        tab 数据仅本地使用：驱动 bili_now_watching 与同步 watcher 的精确感知，
        不上传、不写日志。
        """
        ts = float(at) if at and at > 0 else time.time()
        self._ext_last_seen_ts = ts
        self._ext_tab_data = {
            "url": str(url or "").strip(),
            "title": str(title or "").strip(),
            "at": ts,
        }
        return {"ok": True, "detail": "已记录当前活动标签页"}

    def _ext_tab(self) -> dict | None:
        """返回新鲜（120s 内）的扩展标签页数据；无或过期返回 None。"""
        tab = getattr(self, "_ext_tab_data", None)
        if not tab:
            return None
        if time.time() - float(tab.get("at") or 0) > web_ui._EXT_ONLINE_WINDOW:
            return None
        return tab

    def _panel_status(self) -> dict:
        """面板状态接口：浏览器接入状态 + B站绑定状态 + 扩展通道状态（纯本地，无网络）。"""
        cookie = os.environ.get("BILI_COOKIE", "") or ""
        source = getattr(self, "_cookie_source", _COOKIE_SOURCE_NONE)
        monitor = getattr(self, "_browser_monitor", None) or self._monitor_browser()
        ext_online = (time.time() - self._ext_last_seen()) < web_ui._EXT_ONLINE_WINDOW
        tab = self._ext_tab()
        uid = _cookie_uid(cookie)
        has_csrf = "bili_jct=" in cookie
        return {
            "ok": True,
            "browser": monitor.get("detail", "未接入"),
            "browser_ok": bool(monitor.get("connected")),
            "bound": "SESSDATA=" in cookie,
            "has_csrf": has_csrf,
            "cookie_manual": source == _COOKIE_SOURCE_MANUAL,
            "cookie_source": source,
            "extension_online": ext_online,
            "extension_last_seen": int(self._ext_last_seen()),
            "current_tab": tab,
            "uname": f"uid {uid}" if uid else "",
        }

    def _panel_bind(self) -> dict:
        """面板“一键接入”：用户点击即授权启动调试实例并读取登录态。"""
        result = self._run_bind(auto_launch=True)
        try:
            self._browser_monitor = self._monitor_browser()
        except Exception:
            pass
        return result

    def _ensure_panel(self) -> tuple[str, bool]:
        panel = getattr(self, "_bind_panel", None)
        if panel is None:
            panel = web_ui.BindPanel(
                self._panel_status,
                self._panel_bind,
                ext_fn=self._receive_ext_cookie,
                tab_fn=self._receive_ext_tab,
            )
            self._bind_panel = panel
        return panel.ensure_started()

    @llm_tool(
        name="bili_bind_web",
        description=(
            "打开 B站接入面板（本地网页）：YUI 或用户通过网页完成浏览器接入与 B站绑定，"
            "完全绕开本地进程状态判断——浏览器残留进程（crashpad/后台 chrome.exe 等）不再阻挡接入。"
            "面板同时显示'浏览器扩展（B站接入桥）'通道状态：若已安装扩展并登录 B站，登录态会自动直连同步，无需任何操作。"
            "何时用：用户说'通过网页接入浏览器/网页方式绑定/打开接入面板/自动绑定又不行/扩展装了吗'时。"
            "面板功能：一键启动调试浏览器并读取 B站登录态（不关闭/不删除任何东西）、"
            "实时显示浏览器接入、扩展通道与绑定状态。调用后返回本地面板地址，提示用户打开并点'一键接入'；"
            "若浏览器已开启接入端口或扩展已在线则已自动绑定。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=20.0,
    )
    async def bili_bind_web(self, **_):
        url, started = self._ensure_panel()
        # 顺带触发一次后台静默接入（仅复用已开启的调试端口，不启动浏览器）
        self._bg_bind()
        return {
            "output": (
                f"已打开 B站接入面板：{url}。请在浏览器打开该地址，点'一键接入'完成绑定；"
                "若浏览器已开启接入端口则已自动绑定，刷新面板可见状态。"
            ),
            "is_error": False,
            "panel_url": url,
            "started_now": started,
        }

    @llm_tool(
        name="bili_bind_status",
        description=(
            "查看当前 B站账号绑定与浏览器接入状态。何时用：用户问'你绑定B站了吗/浏览器接入没/为什么看不到弹幕/账号接上了吗'时，"
            "或 YUI 需要自查登录态是否可用。返回：绑定来源（CDP 运行时接入/手动配置/自动读取/未绑定）、浏览器接入状态"
            "（已连接调试浏览器/浏览器未开启接入端口/浏览器未运行）、Cookie 包含哪些凭证（不泄露内容）、"
            "未绑定时给出下一步建议（重新登录/一次性接入设置）。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=15.0,
    )
    async def bili_bind_status(self, **_):
        cookie = os.environ.get("BILI_COOKIE", "") or ""
        source = getattr(self, "_cookie_source", _COOKIE_SOURCE_NONE)
        summary = _cookie_summary(cookie)
        # 浏览器接入监控（最近一次探测结果 + 现场刷新；线程化避免阻塞事件循环）
        monitor = getattr(self, "_browser_monitor", None) or self._monitor_browser()
        try:
            monitor = await asyncio.wait_for(asyncio.to_thread(self._monitor_browser), timeout=6.0)
        except Exception:
            pass
        self._browser_monitor = monitor
        ext_online = (time.time() - self._ext_last_seen()) < web_ui._EXT_ONLINE_WINDOW
        ext_text = "浏览器扩展（B站接入桥）在线，登录态自动同步" if ext_online else "浏览器扩展未在线（可打开接入面板查看/一键接入）"
        m_text = "浏览器已接入（调试实例在线）" if monitor.get("connected") else monitor.get("detail", "浏览器未接入")
        if cookie and "SESSDATA=" in cookie:
            has_csrf = "bili_jct=" in cookie
            csrf_note = ""
            if not has_csrf:
                csrf_note = (
                    "；⚠ 注意：Cookie 缺少 bili_jct（CSRF token），发弹幕/改稿件会失败——"
                    "请在浏览器里退出并重新登录一次 bilibili.com 以刷新凭证（无需关闭浏览器，扩展会自动同步）"
                )
            return {
                "output": f"当前 B站账号已绑定（来源：{summary}）。{m_text}。{ext_text}{csrf_note}",
                "is_error": False,
                "bound": True,
                "source": source,
                "has_csrf": has_csrf,
                "browser_connected": monitor.get("connected"),
                "browser_detail": monitor.get("detail"),
                "extension_online": ext_online,
                "extension_last_seen": int(self._ext_last_seen()),
            }
        # 未绑定：扩展通道优先（全程无需关闭/重启浏览器），CDP 仅兜底检测（线程化 + 8s 预算）
        if ext_online:
            # 扩展在线但未同步到登录态：引导确认浏览器登录，绝不教任何关闭/退出浏览器操作
            return {
                "output": (
                    "浏览器扩展（B站接入桥）在线，但尚未收到 B站登录态（可能是浏览器未登录，或插件刚重启、"
                    "扩展正在自动重连）。无需关闭或重启浏览器：请在浏览器里打开 bilibili.com 确认已登录，"
                    "扩展会自动同步登录态（约 5 秒内）；也可以让我打开接入面板查看扩展通道状态。"
                ),
                "is_error": True,
                "bound": False,
                "source": _COOKIE_SOURCE_NONE,
                "extension_online": True,
                "extension_last_seen": int(self._ext_last_seen()),
            }
        try:
            status, auto_cookie, detail = await asyncio.wait_for(
                asyncio.to_thread(cdp_cookie.bind_via_cdp, False),
                timeout=8.0,
            )
        except Exception:
            status, auto_cookie, detail = cdp_cookie._STATUS_FAIL, "", "CDP 检测超时/异常"
        if auto_cookie and "SESSDATA=" in auto_cookie:
            os.environ["BILI_COOKIE"] = auto_cookie
            self._cookie_source = _COOKIE_SOURCE_AUTO
            return {"output": f"已自动绑定 B站账号（{detail}）。", "is_error": False, "bound": True, "source": _COOKIE_SOURCE_AUTO, "browser_connected": True, "browser_detail": detail}
        hint = ""
        if status == cdp_cookie._STATUS_NOT_LOGGED_IN:
            hint = "浏览器里没有有效的 B站登录态（可能已过期或未登录）：请先打开浏览器登录一次 bilibili.com，再对我说'绑定好了'。"
        elif status == cdp_cookie._STATUS_NO_BROWSER:
            hint = "调试模式浏览器未能启动（默认浏览器正在使用中）。无需关闭浏览器：请确认已安装并重载'B站接入桥'扩展（扩展会随浏览器自动同步登录态），或打开接入面板一键接入，全程不关闭/不重启浏览器。"
        elif status == cdp_cookie._STATUS_FAIL:
            hint = f"接入检测未完成（{detail}）。"
        return {
            "output": f"未绑定 B站账号。{hint}",
            "is_error": True,
            "bound": False,
            "source": _COOKIE_SOURCE_NONE,
            "detect_status": status,
            "browser_connected": monitor.get("connected"),
            "browser_detail": monitor.get("detail"),
            "extension_online": ext_online,
            "extension_last_seen": int(self._ext_last_seen()),
        }

    @llm_tool(
        name="bili_bind_account",
        description=(
            "接入 B站账号（全面扩展/CDP 运行时接入，不依赖进程状态判断）：优先通过'B站接入桥'扩展通道自动"
            "同步浏览器登录态，扩展不可用时再尝试调试模式接入，全程绝不关闭/重启浏览器，浏览器残留进程不会阻挡接入。"
            "何时用：用户说'允许你读取浏览器Cookie/接我的B站账号/绑定账号/确认/绑定好了/接入浏览器'时。"
            "参数 allow（必填布尔）：true=用户已授权自动读取（写入一次永久生效），false=拒绝。"
            "若检测到浏览器未登录/登录态过期，会提示用户先登录 bilibili.com。"
            "这是本地操作：CDP 仅绑定 127.0.0.1，Cookie 不上传、不写日志。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "allow": {"type": "boolean", "description": "用户是否允许自动读取浏览器 Cookie"},
            },
            "required": ["allow"],
        },
        timeout=90.0,
    )
    async def bili_bind_account(self, *, allow: bool = False, **_):
        params = _.get("parameters") or {}
        allow = bool(allow) if isinstance(allow, bool) else bool(params.get("allow"))
        if not allow:
            return {"output": "已保持不读取浏览器 Cookie（仅手动配置可用）。", "is_error": False, "allowed": False}
        # 写入授权（保留已有 settings 其他键）
        try:
            cfg = await self.config.dump(timeout=5.0)
            cfg = cfg if isinstance(cfg, dict) else {}
        except Exception:
            cfg = {}
        section = cfg.get(_CONFIG_SECTION) if isinstance(cfg.get(_CONFIG_SECTION), dict) else {}
        new_section = {**section, _CONFIG_ALLOW_SCAN_KEY: True}
        try:
            await self.config.update({_CONFIG_SECTION: new_section})
        except Exception:
            pass  # 授权写入失败不阻塞本次绑定（下次启动需重新授权）
        # 1) CDP 运行时接入：直接连正在运行的浏览器（用户主动请求时允许启动调试实例，绝不杀进程；
        #    线程化 + 10s 预算，避免阻塞事件循环）
        try:
            status, cookie, detail = await asyncio.wait_for(
                asyncio.to_thread(cdp_cookie.bind_via_cdp, True),
                timeout=10.0,
            )
        except Exception:
            status, cookie, detail = cdp_cookie._STATUS_FAIL, "", "CDP 接入超时/异常"
        if cookie and "SESSDATA=" in cookie:
            os.environ["BILI_COOKIE"] = cookie
            self._cookie_source = _COOKIE_SOURCE_AUTO
            try:
                self._browser_monitor = self._monitor_browser()
            except Exception:
                pass
            return {"output": f"已授权并绑定 B站账号（{detail}）。", "is_error": False, "bound": True, "source": _COOKIE_SOURCE_AUTO}
        # 2) 兜底：浏览器完全未运行时的文件路径读取
        if status == cdp_cookie._STATUS_NO_BROWSER:
            try:
                _file_cookie, _file_detail = cookie_auto.detect_bili_cookie()[1:3]
            except Exception:
                _file_cookie, _file_detail = "", cookie_auto.STATUS_TEXT[cookie_auto._STATUS_FAIL]
            if _file_cookie and "SESSDATA=" in _file_cookie:
                os.environ["BILI_COOKIE"] = _file_cookie
                self._cookie_source = _COOKIE_SOURCE_AUTO
                return {"output": f"已授权并绑定 B站账号（{_file_detail}）。", "is_error": False, "bound": True, "source": _COOKIE_SOURCE_AUTO}
        if status == cdp_cookie._STATUS_NOT_LOGGED_IN:
            return {
                "output": "授权已记录。当前浏览器中没有有效的 B站登录态（可能未登录或已过期）：请先在浏览器里打开 bilibili.com 扫码登录，登录好后对我说'绑定好了'。",
                "is_error": True,
                "bound": False,
                "detect_status": status,
            }
        return {
            "output": f"绑定未完成。{detail}",
            "is_error": True,
            "bound": False,
            "detect_status": status,
        }
