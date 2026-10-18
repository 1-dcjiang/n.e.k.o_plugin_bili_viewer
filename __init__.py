"""B站看板（bili_viewer）— 让 YUI 浏览哔哩哔哩视频/直播/评论/弹幕/字幕。

安全边界：只读公开内容；不登录、不发布评论/弹幕/动态、不下载视频文件；
可选用环境变量 BILI_COOKIE 提升接口可用性，但绝不读写账号数据。
"""

from __future__ import annotations

from plugin.sdk.plugin import NekoPluginBase, lifecycle, llm_tool, neko_plugin

from . import bili


def _wrap_err(exc: bili.BiliError) -> dict:
    return {
        "output": exc.message,
        "is_error": True,
        "error": exc.code,
    }


@neko_plugin
class BiliViewerPlugin(NekoPluginBase):
    """N.E.K.O. 插件：哔哩哔哩内容浏览助手。"""

    def __init__(self, ctx):
        super().__init__(ctx)

    # ══════════ 生命周期 ══════════

    @lifecycle(id="startup")
    async def on_startup(self, **_):
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
