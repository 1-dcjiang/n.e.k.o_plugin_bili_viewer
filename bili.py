"""B站看板 — 哔哩哔哩只读浏览 API 层（纯标准库实现）。

覆盖：视频信息/分P/标签、弹幕（XML）、CC 字幕、AI 概要、评论、搜索、
直播信息、直播实时弹幕（标准库手写 WebSocket）、UP主信息与视频列表。

安全边界：只读公开内容；不登录、不发评论/弹幕/动态、不下载视频文件；
可选用环境变量 BILI_COOKIE（完整 Cookie 字符串，含 SESSDATA）提升部分
接口的可用性，但插件绝不写入或修改任何账号数据。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_API = "https://api.bilibili.com"
_LIVE_API = "https://api.live.bilibili.com"
_TIMEOUT = 12.0

# wbi 签名：mixin key 字符打乱表（公开算法）
_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 25,
    0, 5, 36, 11, 33, 6, 17, 41, 49, 39, 60, 9, 1, 54, 61, 28, 19, 37, 29,
    26, 14, 44, 24, 13, 7, 20, 40, 38, 4, 57, 22, 16, 63, 55, 12, 34, 42,
    48, 21, 59, 30, 52, 62, 56, 51,
]

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_ROOM_RE = re.compile(r"live\.bilibili\.com/(\d+)")


class BiliError(Exception):
    """B站接口错误：message 为用户可读描述，code 为 B站/本地错误码。"""

    def __init__(self, message: str, code: str = "BIZ_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


# ══════════════ 基础 HTTP ══════════════


def _cookie() -> str:
    return os.environ.get("BILI_COOKIE", "").strip()


def _http_get(url: str, params: dict | None = None, timeout: float = _TIMEOUT) -> dict:
    """GET JSON 接口；返回解析后的 dict。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    if _cookie():
        req.add_header("Cookie", _cookie())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise BiliError(f"接口 HTTP {exc.code}", "HTTP_ERROR") from exc
    except urllib.error.URLError as exc:
        raise BiliError(f"网络错误：{exc.reason}", "NETWORK") from exc
    except socket.timeout as exc:
        raise BiliError("请求超时", "TIMEOUT") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BiliError("接口返回非 JSON", "BAD_RESPONSE") from exc
    if data.get("code", 0) != 0:
        code = data.get("code", -1)
        if code in (-352, -401, -412):
            raise BiliError("触发风控限制，请稍后再试（或配置 BILI_COOKIE）", "RISK_CONTROL")
        if code == -404:
            raise BiliError("内容不存在或已被删除", "NOT_FOUND")
        if code == -403:
            raise BiliError("无权限访问", "FORBIDDEN")
        raise BiliError(data.get("message") or data.get("msg") or "B站接口错误", f"BIZ_{code}")
    return data


# ══════════════ wbi 签名 ══════════════


_mixin_cache: str | None = None
_mixin_cache_ts: float = 0.0


def _get_mixin_key() -> str:
    """从 nav 接口获取 img/sub key 并生成 mixin key（进程内缓存 1 小时）。

    注意：未登录时 nav 返回 code=-101，但 data.wbi_img 仍会给出游客密钥，
    因此这里不能用 _http_get（它会在 code!=0 时抛错）。
    """
    global _mixin_cache, _mixin_cache_ts
    now = time.time()
    if _mixin_cache is not None and now - _mixin_cache_ts < 3600:
        return _mixin_cache
    req = urllib.request.Request(_API + "/x/web-interface/nav", headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    if _cookie():
        req.add_header("Cookie", _cookie())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise BiliError(f"接口 HTTP {exc.code}", "HTTP_ERROR") from exc
    except urllib.error.URLError as exc:
        raise BiliError(f"网络错误：{exc.reason}", "NETWORK") from exc
    except socket.timeout as exc:
        raise BiliError("请求超时", "TIMEOUT") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BiliError("接口返回非 JSON", "BAD_RESPONSE") from exc
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img_url = wbi.get("img_url") or ""
    sub_url = wbi.get("sub_url") or ""
    # 兼容新旧两种格式：img_7cd0....png 与 7cd0....png
    img = re.search(r"/(?:img_)?([0-9a-f]{32})\.", img_url)
    sub = re.search(r"/(?:img_)?([0-9a-f]{32})\.", sub_url)
    if not img or not sub:
        raise BiliError("wbi 密钥解析失败（可配置 BILI_COOKIE 后重试）", "WBI_KEY")
    mixin = "".join((img.group(1) + sub.group(1))[i] for i in _MIXIN_TABLE)[:32]
    _mixin_cache, _mixin_cache_ts = mixin, now
    return mixin


def _wbi_sign(params: dict, mixin_key: str) -> dict:
    """给请求参数附加 wts 与 w_rid 签名。"""
    params = dict(params)
    params["wts"] = int(time.time())
    items = sorted(params.items(), key=lambda kv: str(kv[0]))
    query = urllib.parse.urlencode(items)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def _wbi_get(url: str, params: dict, timeout: float = _TIMEOUT) -> dict:
    """带 wbi 签名的 GET。"""
    mixin = _get_mixin_key()
    signed = _wbi_sign(params, mixin)
    return _http_get(url, signed, timeout=timeout)


# ══════════════ 解析辅助 ══════════════


def _parse_bvid(text: str) -> str:
    """从输入提取 BV 号；支持完整 URL。"""
    m = _BVID_RE.search(text)
    if not m:
        raise BiliError("未找到 BV 号（请提供形如 BV1xx... 或 https://www.bilibili.com/video/BV1xx... 的链接）", "BAD_BVID")
    return m.group(1)


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_time(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (ValueError, OSError):
        return str(ts)


# ══════════════ 视频 ══════════════


def video_info(bvid: str) -> dict:
    """视频详情：标题/UP主/时长/分区/互动数据/分P/字幕/标签。"""
    bvid = _parse_bvid(bvid)
    data = (_http_get(_API + "/x/web-interface/view", {"bvid": bvid})).get("data") or {}
    if not data:
        raise BiliError("视频不存在或已被删除", "NOT_FOUND")
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    pages = data.get("pages") or []
    subtitle_list = ((data.get("subtitle") or {}).get("list")) or []
    tags = []
    try:
        tags_data = _http_get(_API + "/x/tag/archive/tags", {"bvid": bvid}).get("data") or []
        tags = [t.get("tag_name", "") for t in tags_data if t.get("tag_name")]
    except BiliError:
        tags = []
    return {
        "bvid": bvid,
        "aid": data.get("aid"),
        "title": data.get("title", "").strip(),
        "desc": (data.get("desc") or "").strip(),
        "pic": data.get("pic", ""),
        "owner_mid": owner.get("mid"),
        "owner_name": owner.get("name", ""),
        "pubdate": data.get("pubdate"),
        "duration": data.get("duration", 0),
        "tname": data.get("tname", ""),
        "view": stat.get("view", 0),
        "danmaku": stat.get("danmaku", 0),
        "reply": stat.get("reply", 0),
        "favorite": stat.get("favorite", 0),
        "coin": stat.get("coin", 0),
        "share": stat.get("share", 0),
        "like": stat.get("like", 0),
        "pages": [
            {"cid": p.get("cid"), "page": p.get("page"), "part": (p.get("part") or "").strip(), "duration": p.get("duration", 0)}
            for p in pages
        ],
        "subtitle_urls": [
            {
                "lan": s.get("lan", ""),
                "lan_doc": s.get("lan_doc", ""),
                "url": s.get("subtitle_url", ""),
            }
            for s in subtitle_list
            if s.get("subtitle_url")
        ],
        "tags": tags,
    }


def format_video_info(info: dict) -> str:
    """把 video_info 渲染为给 YUI 的文本。"""
    parts = info.get("pages") or []
    sub = info.get("subtitle_urls") or []
    lines = [
        f"标题：{info['title']}",
        f"UP主：{info['owner_name']}（mid {info['owner_mid']}）",
        f"BV号：{info['bvid']} ｜ 分区：{info['tname']}",
        f"发布时间：{_fmt_time(info['pubdate'])} ｜ 时长：{_fmt_duration(info['duration'])}",
        f"播放 {info['view']} ｜ 弹幕 {info['danmaku']} ｜ 评论 {info['reply']} ｜ "
        f"点赞 {info['like']} ｜ 投币 {info['coin']} ｜ 收藏 {info['favorite']} ｜ 分享 {info['share']}",
    ]
    if parts:
        first = parts[0]
        lines.append(f"分P：共 {len(parts)} 个，当前第 {first.get('page', 1)} 集「{first.get('part') or '正片'}」（cid {first.get('cid')}）")
    if info.get("desc"):
        lines.append("简介：" + (info["desc"][:200] + "…" if len(info["desc"]) > 200 else info["desc"]))
    if info.get("tags"):
        lines.append("标签：" + "、".join(info["tags"][:10]))
    if sub:
        lines.append("字幕：" + "、".join(f"{s['lan_doc']}({s['lan']})" for s in sub))
    return "\n".join(lines)


def video_danmaku(bvid: str, cid: int | None = None, limit: int = 200) -> list[str]:
    """拉取视频弹幕文本（默认第一P的 cid）。"""
    bvid = _parse_bvid(bvid)
    if cid is None:
        info = video_info(bvid)
        if not info["pages"]:
            raise BiliError("该视频没有可用的分P信息", "NO_CID")
        cid = info["pages"][0]["cid"]
    url = _API + "/x/v1/dm/list.so"
    params = {"oid": cid}
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": _UA, "Referer": f"https://www.bilibili.com/video/{bvid}"},
    )
    if _cookie():
        req.add_header("Cookie", _cookie())
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise BiliError(f"弹幕接口 HTTP {exc.code}", "HTTP_ERROR") from exc
    except urllib.error.URLError as exc:
        raise BiliError(f"网络错误：{exc.reason}", "NETWORK") from exc
    if raw[:2] == b"\x1f\x8b":  # gzip 压缩响应
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise BiliError("弹幕数据解压失败", "DANMAKU_PARSE") from exc
    text = raw.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise BiliError("弹幕接口返回加密/受限数据（游客模式弹幕可能被B站加扰；配置 BILI_COOKIE 后重试）", "DANMAKU_LOCKED") from exc
    danmakus: list[str] = []
    for d in root.findall("d"):
        p = (d.get("p") or "").split(",")
        try:
            ts = float(p[0]) if p and p[0] else 0.0
        except ValueError:
            ts = 0.0
        danmakus.append(f"[{_fmt_duration(int(ts))}] {d.text or ''}".strip())
        if len(danmakus) >= limit:
            break
    if not danmakus:
        raise BiliError("该视频暂无弹幕", "NO_DANMAKU")
    return danmakus


def video_subtitle(bvid: str, cid: int | None = None, lan: str = "zh-CN") -> str:
    """获取视频 CC 字幕文本（默认第一P、简体中文）。"""
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    if cid is None:
        if not info["pages"]:
            raise BiliError("该视频没有分P信息", "NO_CID")
        cid = info["pages"][0]["cid"]
    subs = info["subtitle_urls"]
    if not subs:
        raise BiliError("该视频没有 CC 字幕", "NO_SUBTITLE")
    target = next((s for s in subs if s["lan"] == lan), subs[0])
    url = target["url"]
    if url.startswith("//"):
        url = "https:" + url
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": f"https://www.bilibili.com/video/{bvid}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, ValueError, socket.timeout) as exc:
        raise BiliError(f"字幕获取失败：{exc}", "SUBTITLE_FAIL") from exc
    body = payload.get("body") or []
    if not body:
        raise BiliError("字幕内容为空", "NO_SUBTITLE")
    lines = []
    for item in body:
        content = (item.get("content") or "").strip()
        if content:
            lines.append(content)
    return "\n".join(lines)


def video_ai_summary(bvid: str) -> str:
    """B站官方 AI 视频概要（仅部分视频提供）。"""
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    if not info["pages"]:
        raise BiliError("该视频没有分P信息", "NO_CID")
    cid = info["pages"][0]["cid"]
    try:
        data = _http_get(
            _API + "/x/web-interface/view/conclusion/get",
            {"bvid": bvid, "cid": cid},
        ).get("data") or {}
    except BiliError:
        raise BiliError("该视频暂不提供 AI 概要", "NO_SUMMARY") from None
    model = data.get("model_result") or {}
    summary = (model.get("summary") or "").strip()
    if not summary:
        raise BiliError("该视频暂不提供 AI 概要", "NO_SUMMARY")
    return summary


def video_comments(bvid: str, mode: str = "hot", page: int = 1, limit: int = 15) -> list[dict]:
    """视频评论：mode=hot 热门 / new 最新；返回 rpid/用户/内容/点赞/回复。"""
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    aid = info.get("aid")
    if not aid:
        raise BiliError("视频不存在", "NOT_FOUND")
    mode_code = 3 if mode == "hot" else 2
    data = _wbi_get(
        _API + "/x/v2/reply/main",
        {"type": 1, "oid": aid, "mode": mode_code, "next": max(0, page - 1), "ps": limit},
    ).get("data") or {}
    replies = []
    top = data.get("top_reply")
    if top:
        replies.append(top)
    replies.extend(data.get("replies") or [])
    if not replies:
        raise BiliError("该视频暂无评论", "NO_COMMENTS")
    out = []
    for r in replies[:limit]:
        member = r.get("member") or {}
        content = (r.get("content") or {}).get("message") or ""
        out.append({
            "rpid": r.get("rpid"),
            "user": member.get("uname", ""),
            "content": content.strip(),
            "like": r.get("like", 0),
            "rcount": r.get("rcount", 0),
            "ctime": r.get("ctime", 0),
            "reply_count": len(r.get("replies") or []),
        })
    return out


def search(keyword: str, search_type: str = "video", limit: int = 8) -> list[dict]:
    """搜索视频/直播/用户。search_type: video / user / live。"""
    keyword = keyword.strip()
    if not keyword:
        raise BiliError("请输入搜索关键词", "BAD_KEYWORD")
    type_map = {"video": "video", "user": "bili_user", "live": "live_room"}
    stype = type_map.get(search_type, "video")
    data = _wbi_get(
        _API + "/x/web-interface/wbi/search/type",
        {"keyword": keyword, "search_type": stype, "page": 1},
        timeout=15,
    ).get("data") or {}
    results = data.get("result") or []
    out = []
    for r in results[:limit]:
        if stype == "video":
            out.append({
                "type": "video",
                "bvid": r.get("bvid", ""),
                "title": re.sub(r"<[^>]+>", "", r.get("title", "")),
                "author": r.get("author", ""),
                "play": r.get("play", 0),
                "danmaku": r.get("danmaku", 0),
                "duration": r.get("duration", ""),
                "pubdate": r.get("pubdate", 0),
            })
        elif stype == "bili_user":
            out.append({
                "type": "user",
                "mid": r.get("mid"),
                "uname": r.get("uname", ""),
                "fans": r.get("fans", 0),
                "videos": r.get("videos", 0),
                "sign": r.get("usign", "") or r.get("sign", ""),
            })
        elif stype == "live_room":
            out.append({
                "type": "live",
                "roomid": r.get("roomid"),
                "title": r.get("title", ""),
                "uname": r.get("uname", ""),
                "online": r.get("online", 0),
                "area": r.get("area_name", ""),
                "live_status": r.get("live_status", 0),
            })
    if not out:
        raise BiliError(f"没有找到与「{keyword}」相关的{ {'video': '视频', 'user': '用户', 'live': '直播'}.get(search_type, '内容') }", "NO_RESULT")
    return out


# ══════════════ 直播 ══════════════


def _resolve_room(room: str) -> dict:
    """解析房间号（支持 URL/短号/数字），返回真实 room_id 与直播状态。"""
    room = room.strip()
    m = _ROOM_RE.search(room)
    if m:
        room = m.group(1)
    if not room.isdigit():
        raise BiliError("请输入直播房间号或直播间链接（如 live.bilibili.com/12345）", "BAD_ROOM")
    try:
        data = (_http_get(_LIVE_API + "/room/v1/Room/room_init", {"id": int(room)})).get("data") or {}
    except BiliError as exc:
        raise BiliError("直播间解析失败：" + exc.message, "ROOM_INIT") from exc
    room_id = data.get("room_id")
    if not room_id:
        raise BiliError("直播间不存在", "ROOM_NOT_FOUND")
    return {"room_id": room_id, "live_status": data.get("live_status", 0)}


def live_info(room: str) -> dict:
    """直播间信息：主播/标题/分区/在线/状态/封面/简介。

    主用旧版 room/v1/Room/get_info（免 wbi，未登录可用）；
    主播名通过 acc/info 补充（uid 已知时）。
    """
    resolved = _resolve_room(room)
    room_id = resolved["room_id"]
    try:
        data = (_http_get("https://api.live.bilibili.com/room/v1/Room/get_info", {"room_id": room_id})).get("data") or {}
    except BiliError:
        # 旧接口风控时回退到 web 接口
        try:
            data = (_http_get(_LIVE_API + "/xlive/web-room/v1/index/getInfoByRoom", {"room_id": room_id})).get("data") or {}
            room_info = data.get("room_info") or {}
            anchor = data.get("anchor_info") or {}
            base = anchor.get("base_info") or {}
            live_status = room_info.get("live_status", resolved["live_status"])
            status_text = {0: "未开播", 1: "直播中", 2: "轮播中"}.get(live_status, "未知")
            return {
                "room_id": room_info.get("room_id") or room_id,
                "short_id": room_info.get("short_id"),
                "uid": room_info.get("uid"),
                "uname": room_info.get("uname") or base.get("uname", ""),
                "title": (room_info.get("title") or "").strip(),
                "cover": room_info.get("cover", ""),
                "live_status": live_status,
                "status_text": status_text,
                "online": room_info.get("online", 0),
                "area": room_info.get("area_name", ""),
                "parent_area": room_info.get("parent_area_name", ""),
                "description": (room_info.get("description") or "").strip(),
            }
        except BiliError as exc2:
            raise BiliError("直播间信息获取失败：" + exc2.message, "LIVE_INFO") from exc2
    live_status = data.get("live_status", resolved["live_status"])
    status_text = {0: "未开播", 1: "直播中", 2: "轮播中"}.get(live_status, "未知")
    uid = data.get("uid")
    uname = ""
    if uid:
        try:
            udata = (_http_get(_API + "/x/space/acc/info", {"mid": uid, "jsonp": "jsonp"})).get("data") or {}
            uname = udata.get("name") or ""
        except BiliError:
            uname = ""
    return {
        "room_id": data.get("room_id") or room_id,
        "short_id": data.get("short_id"),
        "uid": uid,
        "uname": uname,
        "title": (data.get("title") or "").strip(),
        "cover": data.get("cover", ""),
        "live_status": live_status,
        "status_text": status_text,
        "online": data.get("online", 0),
        "area": data.get("area_name", ""),
        "parent_area": data.get("parent_area_name", ""),
        "description": (data.get("description") or "").strip(),
    }


def format_live_info(info: dict) -> str:
    lines = [
        f"直播间：{info['title']}",
        f"主播：{info['uname']}（uid {info['uid']}）",
        f"状态：{info['status_text']} ｜ 在线：{info['online']} 人",
        f"分区：{info['parent_area']} / {info['area']}",
        f"房间号：{info['room_id']}（短号 {info['short_id'] or '无'}）",
    ]
    if info.get("description"):
        desc = info["description"]
        lines.append("简介：" + (desc[:150] + "…" if len(desc) > 150 else desc))
    return "\n".join(lines)


# ══════════════ 直播实时弹幕（标准库 WebSocket） ══════════════


def _ws_connect(host: str, port: int, path: str, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    except (ssl.SSLError, OSError):
        sock.close()
        raise BiliError("弹幕服务器 TLS 连接失败", "WS_TLS")
    key = __import__("base64").b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://live.bilibili.com\r\n"
        "\r\n"
    )
    sock.sendall(handshake.encode("ascii"))
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise BiliError("弹幕服务器握手失败", "WS_HANDSHAKE")
        buf += chunk
    head = buf.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 101 " not in head:
        raise BiliError(f"弹幕服务器拒绝连接：{head}", "WS_HANDSHAKE")
    return sock


def _ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x2) -> None:
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock: socket.socket) -> bytes:
    def _read(n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise BiliError("弹幕连接已断开", "WS_CLOSED")
            data += chunk
        return data

    b0, b1 = _read(2)
    fin = b0 & 0x80
    opcode = b0 & 0x0F
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read(8))[0]
    payload = _read(length)
    if opcode == 0x8:  # close
        raise BiliError("弹幕连接被服务器关闭", "WS_CLOSED")
    if opcode == 0x9:  # ping → pong
        _ws_send(sock, payload, opcode=0xA)
        return b""
    if not fin:
        rest = _ws_recv(sock)
        return payload + rest
    return payload


def _bili_pack(body: bytes, op: int, version: int = 1) -> bytes:
    total = 16 + len(body)
    # 头：包长(4B) + 头长(2B=16) + 协议版本(2B) + 操作码(4B) + 序号(4B)
    return struct.pack(">IHHII", total, 16, version, op, 1) + body


def _bili_unpack(buf: bytes) -> list[tuple[int, int, bytes]]:
    """拆解 B站弹幕协议包：返回 [(op, version, body)]。"""
    out = []
    pos = 0
    while pos + 16 <= len(buf):
        (total, header_len, version, op, _seq) = struct.unpack_from(">IHHII", buf, pos)
        if total < 16 or header_len < 16 or pos + total > len(buf):
            break
        body = buf[pos + header_len:pos + total]
        out.append((op, version, body))
        pos += total
        if total <= 0:
            break
    return out


def _danmaku_messages(raw: bytes) -> list[tuple[int, bytes]]:
    """处理 op=5 消息：返回 [(version, body)]；zlib 压缩版递归解压。"""
    result = []
    for op, version, body in _bili_unpack(raw):
        if op != 5:
            continue
        if version == 2:
            try:
                result.extend(_danmaku_messages(zlib.decompress(body)))
            except zlib.error:
                continue
        else:
            result.append((version, body))
    return result


def live_danmaku(room: str, duration: int = 8, limit: int = 60) -> list[dict]:
    """连接直播弹幕服务器采集 duration 秒内的实时弹幕（匿名游客）。"""
    resolved = _resolve_room(room)
    room_id = resolved["room_id"]
    try:
        data = (_http_get(_LIVE_API + "/xlive/web-room/v1/index/getDanmuInfo", {"id": room_id})).get("data") or {}
    except BiliError as exc:
        raise BiliError("弹幕服务器信息获取失败（当前网络可能被B站风控；配置 BILI_COOKIE 后重试）：" + exc.message, "DANMU_INFO") from exc
    token = data.get("token") or ""
    hosts = data.get("host_list") or []
    if not hosts:
        raise BiliError("未获取到弹幕服务器地址", "DANMU_INFO")
    host = hosts[0].get("host", "")
    wss_port = hosts[0].get("wss_port") or 443
    if not host:
        raise BiliError("弹幕服务器地址为空", "DANMU_INFO")
    duration = max(3, min(int(duration or 8), 30))
    sock = None
    collected: list[dict] = []
    try:
        sock = _ws_connect(host, wss_port, "/sub", timeout=10)
        auth_body = json.dumps({
            "uid": 0,
            "roomid": room_id,
            "protover": 2,
            "platform": "web",
            "type": 2,
            "key": token,
        }, separators=(",", ":")).encode()
        _ws_send(sock, _bili_pack(auth_body, op=7, version=1))
        _ws_send(sock, _bili_pack(b"[]", op=2, version=1))
        sock.settimeout(2.0)
        deadline = time.time() + duration
        while time.time() < deadline and len(collected) < limit:
            try:
                raw = _ws_recv(sock)
            except socket.timeout:
                # 心跳保活
                try:
                    _ws_send(sock, _bili_pack(b"[]", op=2, version=1))
                except (OSError, BiliError):
                    break
                continue
            if not raw:
                continue
            for version, body in _danmaku_messages(raw):
                if version == 0:
                    try:
                        msg = json.loads(body)
                    except ValueError:
                        continue
                    if msg.get("cmd") == "DANMU_MSG":
                        info = msg.get("info") or []
                        text = ""
                        try:
                            text = (info[1] or "").strip()
                        except (IndexError, TypeError):
                            pass
                        user = ""
                        try:
                            user = (info[2][1] or "").strip()
                        except (IndexError, TypeError):
                            pass
                        if text:
                            collected.append({"user": user, "text": text})
    except BiliError:
        raise
    except (OSError, ssl.SSLError) as exc:
        raise BiliError(f"弹幕连接失败：{exc}", "WS_ERROR") from exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not collected:
        raise BiliError("采集期间未收到弹幕（直播间可能无人发言或连接被限流）", "NO_DANMAKU")
    return collected


# ══════════════ UP主 ══════════════


def user_info(mid: int) -> dict:
    """UP主信息：昵称/签名/粉丝/关注/视频数/等级。

    用 acc/info + jsonp=jsonp（免 wbi、未登录可用、风控宽松）。
    """
    data = (_http_get(_API + "/x/space/acc/info", {"mid": int(mid), "jsonp": "jsonp"})).get("data") or {}
    if not data:
        raise BiliError("用户不存在", "USER_NOT_FOUND")
    return {
        "mid": data.get("mid"),
        "name": data.get("name", ""),
        "sign": (data.get("sign") or "").strip(),
        "face": data.get("face", ""),
        "fans": data.get("fans", 0),
        "attention": data.get("attention", 0),
        "video_count": data.get("video_count", 0),
        "level": data.get("level", 0),
    }


def format_user_info(info: dict) -> str:
    return (
        f"UP主：{info['name']}（mid {info['mid']}）\n"
        f"粉丝 {info['fans']} ｜ 关注 {info['attention']} ｜ 视频 {info['video_count']} ｜ 等级 Lv{info['level']}\n"
        f"签名：{info['sign'] or '无'}"
    )


def user_videos(mid: int, page: int = 1, limit: int = 15) -> list[dict]:
    """UP主的投稿视频列表（按发布时间）。"""
    data = _wbi_get(
        _API + "/x/space/wbi/arc/search",
        {"mid": int(mid), "ps": min(limit, 30), "pn": max(1, page), "order": "pubdate"},
        timeout=15,
    ).get("data") or {}
    vlist = ((data.get("list") or {}).get("vlist")) or []
    if not vlist:
        raise BiliError("该用户暂无投稿或列表为空", "NO_VIDEOS")
    out = []
    for v in vlist[:limit]:
        out.append({
            "bvid": v.get("bvid", ""),
            "title": v.get("title", ""),
            "play": v.get("play", 0),
            "comment": v.get("comment", 0),
            "duration": v.get("length", ""),
            "pubdate": v.get("pubdate", 0),
        })
    return out
