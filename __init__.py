"""B站全能助手（bili_viewer）— 让 YUI 浏览、感知并管理哔哩哔哩。

能力：
- 浏览：搜索/视频详情/弹幕/字幕/AI概要/评论/直播信息与实时弹幕/UP主信息
- 感知：读取前台窗口，识别用户在看的应用/站点/页面主题（实时同步给 YUI）
- 管理：查看自己账号与稿件、修改稿件信息、直播时回复弹幕（需 BILI_COOKIE 登录态）

安全边界：读操作只访问公开内容；写操作仅限本人账号内容、需登录态与用户确认；
实时感知仅读取本地窗口标题与进程名，不读取页面内容、不做网络传输。
"""

from __future__ import annotations

import os
import threading
import time

from plugin.sdk.plugin import NekoPluginBase, lifecycle, llm_tool, neko_plugin

from . import bili, cookie_auto


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

# 插件配置段与 Cookie 配置键
_CONFIG_SECTION = "settings"
_CONFIG_COOKIE_KEY = "bili_cookie"
_CONFIG_ALLOW_SCAN_KEY = "allow_cookie_scan"

_COOKIE_SOURCE_MANUAL = "manual"
_COOKIE_SOURCE_AUTO = "auto"
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


def _sync_text(parsed: dict) -> str | None:
    """把识别结果渲染为注入 YUI 上下文的一句话。"""
    kind = parsed.get("kind")
    if kind == "unknown":
        return None
    if parsed.get("is_bili"):
        label = "直播" if kind == "live" else "视频"
        subject = parsed.get("subject") or "未知内容"
        return f"（实时感知：用户当前正在看 B站{label}「{subject}」）"
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
        while True:
            try:
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

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        # 1) 手动配置（settings.bili_cookie / 环境变量 BILI_COOKIE）
        try:
            cfg = await self.config.dump(timeout=5.0)
            cfg = cfg if isinstance(cfg, dict) else {}
        except Exception:
            cfg = {}
        cookie = _apply_cookie_from_config(cfg, os.environ)
        source = _COOKIE_SOURCE_MANUAL if cookie else _COOKIE_SOURCE_NONE
        # 2) 未手动配置且用户已授权 → 自动检测浏览器（Chrome/Edge）已登录的 B站 Cookie
        if not cookie and _allow_cookie_scan(cfg):
            status, auto_cookie, _detail = cookie_auto.detect_bili_cookie()
            if auto_cookie:
                cookie = auto_cookie
                source = _COOKIE_SOURCE_AUTO
        if cookie:
            os.environ["BILI_COOKIE"] = cookie
        self._cookie_source = source
        self._start_sync_watcher()
        return {"status": "ready", "plugin": "bili_viewer"}

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
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
            "（标题/UP主/播放/弹幕/BV号/简介）。"
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
            info = bili.now_watching()
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
            "且仅在用户明确确认后调用。"
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

    # ══════════ 账号绑定（自动检测浏览器 Cookie） ══════════

    @llm_tool(
        name="bili_bind_status",
        description=(
            "查看当前 B站账号绑定状态。何时用：用户问'你绑定B站了吗/为什么看不到弹幕/账号接上了吗'时，"
            "或 YUI 需要自查登录态是否可用。返回：绑定来源（手动配置/自动读取浏览器 Cookie/未绑定）、"
            "Cookie 包含哪些凭证（不泄露内容）、未绑定时给出下一步建议（关闭浏览器/先登录 B站）。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=10.0,
    )
    async def bili_bind_status(self, **_):
        cookie = os.environ.get("BILI_COOKIE", "") or ""
        source = getattr(self, "_cookie_source", _COOKIE_SOURCE_NONE)
        summary = _cookie_summary(cookie)
        if cookie and "SESSDATA=" in cookie:
            output = f"当前 B站账号已绑定（来源：{'自动读取浏览器 Cookie' if source == _COOKIE_SOURCE_AUTO else '手动配置'}），{summary}。"
            return {"output": output, "is_error": False, "bound": True, "source": source}
        if source == _COOKIE_SOURCE_AUTO:
            return {"output": "已启用自动读取浏览器 Cookie，但当前未检测到 B站登录态。", "is_error": True, "bound": False, "source": source}
        # 未绑定：尝试自动检测并报告原因
        try:
            status, auto_cookie, detail = cookie_auto.detect_bili_cookie()
        except Exception:
            status, auto_cookie, detail = cookie_auto._STATUS_FAIL, "", cookie_auto.STATUS_TEXT[cookie_auto._STATUS_FAIL]
        if auto_cookie:
            os.environ["BILI_COOKIE"] = auto_cookie
            self._cookie_source = _COOKIE_SOURCE_AUTO
            return {"output": "已自动绑定 B站账号（读取浏览器 Cookie）。", "is_error": False, "bound": True, "source": _COOKIE_SOURCE_AUTO}
        return {
            "output": f"未绑定 B站账号。原因：{detail}。"
            + ("若用户同意，可调用 bili_bind_account(allow=true) 授权自动读取浏览器 Cookie。" if not _allow_cookie_scan({}) else ""),
            "is_error": True,
            "bound": False,
            "source": _COOKIE_SOURCE_NONE,
            "detect_status": status,
        }

    @llm_tool(
        name="bili_bind_account",
        description=(
            "接入 B站账号：授权插件自动读取本机浏览器（Chrome/Edge）已登录的 B站 Cookie 并绑定。"
            "何时用：用户说'允许你读取浏览器Cookie/接我的B站账号/绑定账号/确认'时。"
            "参数 allow（必填布尔）：true=用户已授权自动读取（写入一次永久生效），false=拒绝。"
            "参数 force_close（可选布尔）：true=用户同意由插件自动结束浏览器进程（chrome.exe/msedge.exe）"
            "再重新检测绑定，适合浏览器窗口关了但后台进程仍在（Cookie 被锁）的场景；"
            "注意会关闭浏览器所有窗口（未保存内容可能丢失），仅在用户明确同意时传 true。"
            "授权后立即检测：浏览器已关闭时直接绑定；浏览器运行中且未同意自动关闭时，"
            "提示用户关闭浏览器或同意自动关闭后重试。这是本地操作，无网络传输，Cookie 不上传不写日志。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "allow": {"type": "boolean", "description": "用户是否允许自动读取浏览器 Cookie"},
                "force_close": {"type": "boolean", "description": "用户是否同意插件自动结束浏览器进程后重试（可选，默认 false）"},
            },
            "required": ["allow"],
        },
        timeout=60.0,
    )
    async def bili_bind_account(self, *, allow: bool = False, force_close: bool = False, **_):
        params = _.get("parameters") or {}
        allow = bool(allow) if isinstance(allow, bool) else bool(params.get("allow"))
        if isinstance(force_close, bool) is False:
            force_close = bool(params.get("force_close"))
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
        try:
            status, cookie, detail = cookie_auto.detect_bili_cookie()
        except Exception:
            status, cookie, detail = cookie_auto._STATUS_FAIL, "", cookie_auto.STATUS_TEXT[cookie_auto._STATUS_FAIL]
        if status == cookie_auto._STATUS_LOCKED and force_close:
            # 用户同意自动关闭浏览器：结束进程后重试绑定
            try:
                status, cookie, detail = cookie_auto.detect_after_kill()
            except Exception:
                status, cookie, detail = cookie_auto._STATUS_FAIL, "", cookie_auto.STATUS_TEXT[cookie_auto._STATUS_FAIL]
        if cookie:
            os.environ["BILI_COOKIE"] = cookie
            self._cookie_source = _COOKIE_SOURCE_AUTO
            return {"output": f"已授权并自动绑定 B站账号（{detail}）。", "is_error": False, "bound": True, "source": _COOKIE_SOURCE_AUTO}
        return {
            "output": f"授权已记录（下次启动自动生效）。当前检测：{detail}"
            + ("。若同意由插件自动关闭浏览器后重试，可对我说'允许自动关闭浏览器并绑定'。" if status == cookie_auto._STATUS_LOCKED else ""),
            "is_error": True,
            "bound": False,
            "detect_status": status,
        }
