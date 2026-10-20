"""B站看板 — 哔哩哔哩只读浏览 API 层（纯标准库实现）。

覆盖：视频信息/分P/标签、弹幕（seg.so protobuf，XML 兜底）、CC 字幕、AI 概要、
评论、搜索、直播信息、直播实时弹幕（标准库手写 WebSocket）、UP主信息与视频列表。

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


def _proto_fields(data: bytes) -> list[tuple[int, int, int | None, bytes | None]]:
    """轻量 protobuf wire 解析：返回 [(field_no, wire_type, varint值, 原始bytes)]。"""
    out: list[tuple[int, int, int | None, bytes | None]] = []
    pos = 0
    n = len(data)
    while pos < n:
        key = 0
        shift = 0
        while pos < n:
            b = data[pos]
            pos += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        no, wire = key >> 3, key & 7
        if wire == 0:
            val = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            out.append((no, wire, val, None))
        elif wire == 1:
            out.append((no, wire, None, data[pos:pos + 8]))
            pos += 8
        elif wire == 2:
            ln = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            out.append((no, wire, None, data[pos:pos + ln]))
            pos += ln
        elif wire == 5:
            out.append((no, wire, None, data[pos:pos + 4]))
            pos += 4
        else:
            break
    return out


def _parse_danmaku_proto(raw: bytes) -> list[tuple[int, str]]:
    """解析 B站弹幕 protobuf（seg.so 新版响应）。

    顶层 = repeated DanmakuElem（field 1 嵌套消息，wire 2）；
    每条弹幕：文本 = field 7（string），进度 = field 15（varint，秒）。
    """
    items: list[tuple[int, str]] = []
    for no, wire, _v, blob in _proto_fields(raw):
        if no != 1 or wire != 2 or not blob:
            continue
        text: str | None = None
        progress = 0
        for sno, swire, sval, sblob in _proto_fields(blob):
            if sno == 7 and swire == 2 and sblob:
                try:
                    text = sblob.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            elif sno == 15 and swire == 0 and sval is not None:
                progress = int(sval)
        if text:
            items.append((progress, text))
    return items


def _fetch_raw(url: str, params: dict) -> bytes:
    """带 UA/Referer/Cookie 的原始 GET，自动解 gzip。"""
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/video/"},
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
    return raw


def video_danmaku(bvid: str, cid: int | None = None, limit: int = 200) -> list[str]:
    """拉取视频弹幕文本（默认第一P的 cid）。

    优先新接口 x/v2/dm/web/seg.so（protobuf，游客可用）；分 P 逐段采集；
    老接口 dm/list.so（XML）保留为兜底。
    """
    bvid = _parse_bvid(bvid)
    if cid is None:
        info = video_info(bvid)
        if not info["pages"]:
            raise BiliError("该视频没有可用的分P信息", "NO_CID")
        cid = info["pages"][0]["cid"]
    danmakus: list[str] = []
    # 新接口 seg.so：protobuf 弹幕，逐段采集直到 limit
    try:
        for seg in range(1, 8):
            raw = _fetch_raw(_API + "/x/v2/dm/web/seg.so", {"type": 1, "oid": cid, "segment_index": seg})
            items = _parse_danmaku_proto(raw)
            if not items:
                break
            for progress, text in items:
                danmakus.append(f"[{_fmt_duration(int(progress))}] {text}")
                if len(danmakus) >= limit:
                    break
            if len(danmakus) >= limit:
                break
    except BiliError:
        pass
    if not danmakus:
        # 老接口兜底：dm/list.so（部分情况仍返回 XML）
        try:
            raw = _fetch_raw(_API + "/x/v1/dm/list.so", {"oid": cid})
            text = raw.decode("utf-8", errors="replace")
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            if root is not None:
                for d in root.findall("d"):
                    p = (d.get("p") or "").split(",")
                    try:
                        ts = float(p[0]) if p and p[0] else 0.0
                    except ValueError:
                        ts = 0.0
                    danmakus.append(f"[{_fmt_duration(int(ts))}] {d.text or ''}".strip())
                    if len(danmakus) >= limit:
                        break
        except BiliError:
            pass
    if not danmakus:
        raise BiliError("该视频暂无弹幕（游客模式弹幕可能被B站加扰；配置 BILI_COOKIE 后重试）", "NO_DANMAKU")
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
    """B站官方 AI 视频概要（仅部分视频提供，游客不可用，需登录态）。"""
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    if not info["pages"]:
        raise BiliError("该视频没有分P信息", "NO_CID")
    cid = info["pages"][0]["cid"]
    try:
        data = _wbi_get(
            _API + "/x/web-interface/view/conclusion/get",
            {"bvid": bvid, "cid": cid},
        ).get("data") or {}
    except BiliError:
        raise BiliError("AI 概要需要登录态（游客不可用；配置 BILI_COOKIE 后重试）", "NO_SUMMARY") from None
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
        # 旧接口风控时回退到 web 接口（需 wbi 签名；配置 BILI_COOKIE 后可用）
        try:
            data = (_wbi_get(_LIVE_API + "/xlive/web-room/v1/index/getInfoByRoom", {"room_id": room_id})).get("data") or {}
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
        # getDanmuInfo 需要 wbi 签名；游客密钥可获取，仍失败通常是网络 IP 被风控（-352）
        data = (_wbi_get(_LIVE_API + "/xlive/web-room/v1/index/getDanmuInfo", {"id": room_id})).get("data") or {}
    except BiliError as exc:
        raise BiliError("弹幕服务器信息获取失败（当前网络可能被B站风控；配置 BILI_COOKIE 或更换网络后重试）：" + exc.message, "DANMU_INFO") from exc
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

    用 acc/info + jsonp=jsonp（免 wbi、未登录可用）；当前网络被 B站限流时
    给出明确提示（配置 BILI_COOKIE 可提升）。
    """
    try:
        data = (_http_get(_API + "/x/space/acc/info", {"mid": int(mid), "jsonp": "jsonp"})).get("data") or {}
    except BiliError as exc:
        raise BiliError(
            "用户信息获取失败（当前网络可能被B站限流；配置 BILI_COOKIE 后重试）：" + exc.message,
            "USER_INFO",
        ) from exc
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


# ══════════════ 我的账号 / 稿件管理（需 BILI_COOKIE 登录态） ══════════════


def _csrf() -> str:
    """从 Cookie 中提取 bili_jct（写操作的 CSRF token）。"""
    m = re.search(r"bili_jct=([0-9a-zA-Z]+)", _cookie())
    return m.group(1) if m else ""


def _require_login() -> str:
    cookie = _cookie()
    if not cookie:
        raise BiliError("需要登录态：请配置环境变量 BILI_COOKIE（含 SESSDATA 与 bili_jct）", "NEED_LOGIN")
    return cookie


def _http_post(url: str, form: dict, timeout: float = _TIMEOUT) -> dict:
    """POST 表单（带 Cookie）；解析 JSON。"""
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.bilibili.com",
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
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BiliError("接口返回非 JSON", "BAD_RESPONSE") from exc
    if data.get("code", 0) != 0:
        code = data.get("code", -1)
        if code in (-352, -401, -412, -101):
            raise BiliError("登录态失效或触发风控（请重新配置有效的 BILI_COOKIE）", "AUTH_FAILED")
        raise BiliError(data.get("message") or data.get("msg") or "B站接口错误", f"BIZ_{code}")
    return data


def my_info() -> dict:
    """当前账号（BILI_COOKIE）信息：昵称/uid/等级/硬币/大会员。"""
    _require_login()
    data = (_http_get(_API + "/x/web-interface/nav")).get("data") or {}
    if not data.get("mid"):
        raise BiliError("无法读取账号信息（登录态无效）", "AUTH_FAILED")
    level_info = data.get("level_info") or {}
    vip = data.get("vipStatus", 0)
    return {
        "mid": data.get("mid"),
        "uname": data.get("uname", ""),
        "level": level_info.get("current_level", 0),
        "coins": data.get("money", 0),
        "vip": bool(vip),
        "following": data.get("following", 0),
        "follower": data.get("follower", 0),
    }


def format_my_info(info: dict) -> str:
    vip = "大会员" if info["vip"] else "普通用户"
    return (
        f"当前账号：{info['uname']}（uid {info['mid']}）\n"
        f"等级 Lv{info['level']} ｜ {vip} ｜ 硬币 {info['coins']}\n"
        f"关注 {info['following']} ｜ 粉丝 {info['follower']}"
    )


def my_videos(page: int = 1, limit: int = 10) -> list[dict]:
    """当前账号的投稿列表（nav 取 uid → arc/search，需 BILI_COOKIE）。

    注：创作中心 /x/vu/web/video/list 接口已失效（404），改用空间投稿接口。
    """
    _require_login()
    mid = my_info()["mid"]
    try:
        data = _wbi_get(
            _API + "/x/space/wbi/arc/search",
            {"mid": int(mid), "ps": min(limit, 30), "pn": max(1, page), "order": "pubdate"},
            timeout=15,
        ).get("data") or {}
    except BiliError as exc:
        raise BiliError("稿件列表获取失败（当前网络可能被B站风控；请重新配置有效的 BILI_COOKIE）：" + exc.message, "MY_VIDEOS") from exc
    vlist = ((data.get("list") or {}).get("vlist")) or []
    if not vlist:
        raise BiliError("暂无投稿或列表为空", "NO_VIDEOS")
    out = []
    for v in vlist[:limit]:
        state = v.get("state", 0)
        state_text = {0: "公开", -2: "删除", -1: "审核中", 1: "审核中"}.get(state, f"状态{state}")
        out.append({
            "bvid": v.get("bvid", ""),
            "aid": v.get("aid"),
            "title": v.get("title", "").strip(),
            "play": v.get("play", 0),
            "state": state,
            "state_text": state_text,
            "pubdate": v.get("pubdate", 0),
            "length": v.get("length", ""),
        })
    return out


def format_my_videos(videos: list[dict]) -> str:
    lines = []
    for i, v in enumerate(videos, 1):
        lines.append(
            f"{i}. {v['title']}（{v['state_text']}，播放 {v['play']}，时长 {v['length']}）BV号 {v['bvid']}"
        )
    return "\n".join(lines) if lines else "暂无投稿"


def edit_video(bvid: str, title: str | None = None, desc: str | None = None, tag: str | None = None) -> dict:
    """修改自己稿件的信息（标题/简介/标签）。需 BILI_COOKIE 登录态。

    只允许修改属于当前账号的稿件；服务端会校验所有权。
    """
    _require_login()
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    aid = info.get("aid")
    if not aid:
        raise BiliError("无法获取稿件编号", "NO_AID")
    form = {
        "aid": int(aid),
        "vfe": "1",
        "csrf": _csrf(),
        "title": (title if title is not None else info["title"]).strip(),
        "desc": (desc if desc is not None else info["desc"]).strip(),
        "tag": (tag if tag is not None else ",".join(info["tags"])).strip(),
    }
    if not form["csrf"]:
        raise BiliError("Cookie 缺少 bili_jct（CSRF token），无法执行写操作", "NO_CSRF")
    try:
        _http_post(_API + "/x/vu/edit", form)
    except BiliError as exc:
        raise BiliError("稿件修改失败：" + exc.message, "EDIT_FAILED") from exc
    return {"bvid": bvid, "title": form["title"], "desc": form["desc"], "tag": form["tag"], "ok": True}


def live_send_danmaku(room: str, text: str) -> dict:
    """向直播间发送一条弹幕。需 BILI_COOKIE 登录态（含 bili_jct）。

    注意：msg/send 的 roomid 必须是页面显示的房间号（URL 上的数字，
    与 B站 Web 端一致），不能用 room_init 解析出的真实 room_id——
    两者不一致时弹幕会发进错误房间（v0.6.30）。
    """
    _require_login()
    text = (text or "").strip()
    if not text:
        raise BiliError("弹幕内容为空", "EMPTY_MSG")
    if len(text) > 30:
        raise BiliError("弹幕最长 30 个字符", "MSG_TOO_LONG")
    csrf = _csrf()
    if not csrf:
        raise BiliError("Cookie 缺少 bili_jct（CSRF token），无法发送弹幕", "NO_CSRF")
    page_room = room.strip()
    m = _ROOM_RE.search(page_room)
    if m:
        page_room = m.group(1)
    if not page_room.isdigit():
        raise BiliError("请输入直播房间号或直播间链接（如 live.bilibili.com/12345）", "BAD_ROOM")
    form = {
        "roomid": int(page_room),
        "msg": text,
        "csrf": csrf,
        "csrf_token": csrf,
        "rnd": str(int(time.time())),
        "color": "16777215",
        "fontsize": "25",
        "mode": "1",
        "platform": "web",
        "web_location": "1550101",
    }
    # 直播弹幕走 /msg/send（xlive/web-room/v1/dms/send 已废弃返回 404，v0.6.29）
    try:
        data = _http_post(_LIVE_API + "/msg/send", form)
    except BiliError as exc:
        raise BiliError("弹幕发送失败：" + exc.message, "SEND_FAILED:" + exc.code) from exc
    resp_data = data.get("data")
    dmid = ""
    if isinstance(resp_data, dict):
        dmid = str(resp_data.get("dmid") or resp_data.get("dmid_str") or "")
    return {
        "ok": True,
        "room_id": int(page_room),
        "text": text,
        "server": data.get("message") or "",
        "resp_code": data.get("code"),
        "dmid": dmid,
    }


def send_video_danmaku(bvid: str, text: str, progress_ms: int = 0) -> dict:
    """向视频投递一条普通弹幕（默认出现在视频开头时间点）。需 BILI_COOKIE 登录态。

    参数：
        bvid：视频 BV 号（自动解析到第一P 的 cid）。
        text：弹幕内容（最长 100 字）。
        progress_ms：弹幕出现的时间点（毫秒，0=开头）。
    """
    _require_login()
    text = (text or "").strip()
    if not text:
        raise BiliError("弹幕内容为空", "EMPTY_MSG")
    if len(text) > 100:
        raise BiliError("视频弹幕最长 100 个字符", "MSG_TOO_LONG")
    csrf = _csrf()
    if not csrf:
        raise BiliError("Cookie 缺少 bili_jct（CSRF token），无法发送弹幕", "NO_CSRF")
    bvid = _parse_bvid(bvid)
    info = video_info(bvid)
    pages = info.get("pages") or []
    if not pages:
        raise BiliError("该视频没有可用的分P信息", "NO_CID")
    cid = pages[0]["cid"]
    try:
        progress_ms = int(progress_ms)
    except (TypeError, ValueError):
        progress_ms = 0
    progress_ms = max(0, progress_ms)
    form = {
        "type": "1",
        "oid": int(cid),
        "bvid": bvid,
        "msg": text,
        "progress": int(progress_ms),
        "mode": "1",
        "color": "16777215",
        "fontsize": "25",
        "pool": "0",
        "rnd": str(int(time.time()) * 1000000),
        "csrf": csrf,
        "csrf_token": csrf,
        "platform": "web",
    }
    # 新版发送接口要求 wbi 签名（缺失会触发 -352 风控"请求错误"）
    try:
        form = _wbi_sign(form, _get_mixin_key())
    except BiliError:
        pass  # 签名失败不致命，回退裸请求
    try:
        data = _http_post(_API + "/x/v2/dm/post", form)
    except BiliError as exc:
        raise BiliError("视频弹幕发送失败：" + exc.message, "SEND_FAILED:" + exc.code) from exc
    return {"ok": True, "bvid": bvid, "cid": int(cid), "text": text, "progress_ms": int(progress_ms), "server": data.get("message") or ""}


# ══════════════ 当前观看感知（前台窗口标题，通用） ══════════════

_BROWSER_MARKERS = ("chrome", "edge", "firefox", "opera", "brave", "browser", "safari", "360")
_SITE_KEYWORDS = [
    ("bilibili", ("哔哩哔哩", "bilibili")),
    ("youtube", ("youtube",)),
    ("zhihu", ("知乎", "zhihu")),
    ("weibo", ("微博", "weibo")),
    ("xiaohongshu", ("小红书", "xiaohongshu")),
    ("douyin", ("抖音", "douyin")),
    ("baidu", ("百度", "baidu")),
    ("taobao", ("淘宝", "taobao")),
    ("jd", ("京东", "jd")),
    ("tieba", ("贴吧", "tieba")),
    ("github", ("github",)),
    ("douban", ("豆瓣", "douban")),
    ("steam", ("steam",)),
    ("netease", ("网易", "163.com")),
    ("tencent", ("腾讯", "qq.com")),
    ("dingtalk", ("钉钉", "dingtalk")),
    ("feishu", ("飞书", "feishu", "lark")),
    ("wechat", ("微信", "weixin", "wechat")),
]
_BILI_TITLE_SUFFIX_RE = re.compile(r"^(.*?)[_\-—]哔哩哔哩")
# B站新版视频页标题常见格式「视频标题_分区名」（不带"哔哩哔哩"字样），用于兜底识别
_BILI_SECTION_MARKERS = (
    "游戏杂谈", "游戏热门视频", "单机游戏", "电子竞技", "手机游戏", "网络游戏", "桌游棋牌",
    "动画", "番剧", "影视", "电影", "纪录片", "音乐", "舞蹈", "知识", "科技", "数码",
    "生活", "美食", "动物圈", "时尚", "运动", "汽车", "鬼畜", "娱乐", "综艺", "资讯",
    "Vlog", "专栏", "公开课", "搞笑", "日常", "手工", "绘画", "历史", "军事", "游戏",
)
_BILI_SECTION_SUFFIX_RE = re.compile(r"[_\-—](?:" + "|".join(map(re.escape, _BILI_SECTION_MARKERS)) + r")$")
_BROWSER_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-—_]\s*(?:Google Chrome|Chrome|Microsoft Edge|Edge|Firefox|Opera|Brave|Safari|浏览器)$",
    re.IGNORECASE,
)


def _strip_browser_suffix(title: str) -> str:
    """去掉浏览器窗口标题末尾的浏览器名，还原页面标题。"""
    return _BROWSER_TITLE_SUFFIX_RE.sub("", title).strip()


_URL_BVID_RE = re.compile(r"/video/(BV[a-zA-Z0-9]+)")
_URL_AV_RE = re.compile(r"/video/av(\d+)")
_URL_LIVE_RE = re.compile(r"live\.bilibili\.com/(\d+)")
_URL_EP_RE = re.compile(r"bangumi/play/ep(\d+)")
_URL_SS_RE = re.compile(r"bangumi/play/ss(\d+)")


def parse_bili_url(url: str, title: str = "") -> dict | None:
    """从浏览器扩展推送的活动标签页 URL 解析 B站内容。纯函数，可测试。

    返回与 parse_window_title 兼容的 dict（is_bili=True），并附带 bvid / room_id /
    ep / ss 精确标识；非 B站 URL 返回 None。
    """
    url = (url or "").strip()
    if not url:
        return None
    low = url.lower()
    if "bilibili.com" not in low and "b23.tv" not in low:
        return None
    subject = (title or "").strip()
    m = _BILI_TITLE_SUFFIX_RE.match(subject)
    if m and m.group(1).strip():
        subject = m.group(1).strip()
    base = {"app": "browser-ext", "window": title, "is_browser": True, "site": "bilibili", "subject": subject}
    m = _URL_LIVE_RE.search(low)
    if m:
        return {**base, "kind": "live", "is_bili": True, "room_id": m.group(1)}
    m = _URL_BVID_RE.search(url)  # BV 号区分大小写，需在原始 URL 上匹配
    if m:
        return {**base, "kind": "video", "is_bili": True, "bvid": m.group(1)}
    m = _URL_AV_RE.search(low)
    if m:
        return {**base, "kind": "video", "is_bili": True, "aid": m.group(1)}
    m = _URL_EP_RE.search(low)
    if m:
        return {**base, "kind": "bangumi", "is_bili": True, "ep": m.group(1)}
    m = _URL_SS_RE.search(low)
    if m:
        return {**base, "kind": "bangumi", "is_bili": True, "ss": m.group(1)}
    if "live" in low:
        return {**base, "kind": "live", "is_bili": True}
    return {**base, "kind": "web", "is_bili": True}


def parse_window_title(title: str, exe: str = "") -> dict:
    """解析前台窗口标题与进程名，识别应用/站点/页面主题。纯函数，可测试。

    返回：app（进程名）、window（原始标题）、is_browser、site（站点标识）、
    subject（页面/窗口主题）、kind（video/live/web/app/other/unknown）、is_bili。
    """
    title = (title or "").strip()
    exe_name = os.path.basename((exe or "").replace("\\", "/")).lower()
    if not title and not exe_name:
        return {"app": "", "window": "", "is_browser": False, "site": "", "subject": "", "kind": "unknown", "is_bili": False}
    low = title.lower()
    is_browser = any(m in exe_name for m in _BROWSER_MARKERS) or any(
        m in low for m in ("google chrome", "microsoft edge", "firefox", "opera", "brave")
    )
    # 站点关键词只在标题中匹配（不匹配进程名，避免 steam++.exe/Watt Toolkit 这类误判为 steam）
    site = ""
    for name, kws in _SITE_KEYWORDS:
        if any(k in low for k in kws):
            site = name
            break
    # B站：视频页/直播页特殊解析
    if "哔哩哔哩" in title or "bilibili" in low:
        if "直播" in title:
            subject = re.split(r"[_\-—]", title, maxsplit=1)[0].strip()
            return {
                "app": exe_name, "window": title, "is_browser": is_browser, "site": "bilibili",
                "subject": subject, "kind": "live", "is_bili": True,
            }
        m = _BILI_TITLE_SUFFIX_RE.match(title)
        if m and m.group(1).strip():
            return {
                "app": exe_name, "window": title, "is_browser": is_browser, "site": "bilibili",
                "subject": m.group(1).strip(), "kind": "video", "is_bili": True,
            }
        return {
            "app": exe_name, "window": title, "is_browser": is_browser, "site": "bilibili",
            "subject": "", "kind": "other", "is_bili": True,
        }
    # B站兜底：浏览器标题形如「视频标题_分区名」（新版页面不带"哔哩哔哩"字样）
    if is_browser and not site:
        stripped = _strip_browser_suffix(title)
        m = _BILI_SECTION_SUFFIX_RE.search(stripped)
        if m:
            subject = stripped[: m.start()].strip(" _-")
            if subject:
                return {
                    "app": exe_name, "window": title, "is_browser": is_browser, "site": "bilibili",
                    "subject": subject, "kind": "video", "is_bili": True,
                }
    subject = _strip_browser_suffix(title) if is_browser else title
    return {
        "app": exe_name, "window": title, "is_browser": is_browser, "site": site,
        "subject": subject, "kind": "web" if is_browser else "app", "is_bili": False,
    }


def _foreground_window() -> dict:
    """读取当前前台窗口的标题与进程路径（仅 Windows 可用）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, ImportError, OSError):
        raise BiliError("当前平台不支持读取前台窗口", "PLATFORM")
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"title": "", "exe": ""}
    buf = ctypes.create_unicode_buffer(512)
    n = user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value[: max(0, n)]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(512)
            path = ctypes.create_unicode_buffer(512)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                exe = path.value
        finally:
            kernel32.CloseHandle(handle)
    return {"title": title, "exe": exe}


# ══════════ 多窗口感知（实时同步 / bili_now_watching 共用） ══════════
#
# 为什么不用"前台窗口"：用户和 YUI 聊天时，聊天窗口（N.E.K.O 客户端）必然在前台，
# 但用户真正在看的内容（B站视频/网页/游戏）往往在后台。因此改为枚举所有可见窗口，
# 排除聊天客户端/终端/空标题，再按内容优先级选出"最可能在看"的窗口。

# 开发终端/命令行：多为调试或 AI 操作，不代表用户在看的内容
_TERMINAL_EXES = (
    "windowsterminal", "cmd.exe", "powershell", "pwsh",
    "python", "py.exe", "bash", "sh.exe", "wt.exe", "conhost",
)
# N.E.K.O 客户端自身（用户与 YUI 聊天的窗口）：不作为感知对象
_NEKO_CLIENT_EXES = ("n.e.k.o", "neko", "launcher")


def _is_terminal_window(parsed: dict) -> bool:
    """是否开发终端/命令行窗口（不参与感知）。"""
    app = (parsed.get("app") or "").lower()
    return any(m in app for m in _TERMINAL_EXES)


def _is_neko_client(parsed: dict) -> bool:
    """是否 N.E.K.O 客户端聊天窗口（不参与感知）。"""
    app = (parsed.get("app") or "").lower()
    return any(m in app for m in _NEKO_CLIENT_EXES)


def _skip_window(parsed: dict) -> bool:
    """窗口是否应被排除（未知/空标题/终端/聊天客户端）。"""
    if parsed.get("kind") in ("", "unknown"):
        return True
    if not parsed.get("window"):
        return True
    if _is_terminal_window(parsed) or _is_neko_client(parsed):
        return True
    return False


def _window_score(parsed: dict) -> int:
    """内容优先级：B站视频/直播 > 已知站点 > 网页 > 其他应用。"""
    if parsed.get("is_bili"):
        return 100
    if parsed.get("site"):
        return 90
    if parsed.get("kind") == "web":
        return 80
    if parsed.get("kind") == "app":
        return 70
    return 60


def _visible_windows() -> list[dict]:
    """枚举所有可见顶层窗口（仅 Windows），返回 [{"title": str, "exe": str}]。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, ImportError, OSError):
        raise BiliError("当前平台不支持读取窗口", "PLATFORM")

    windows: list[dict] = []

    def _exe_of(pid: int) -> str:
        exe = ""
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                size = wintypes.DWORD(512)
                path = ctypes.create_unicode_buffer(512)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    exe = path.value
            finally:
                kernel32.CloseHandle(handle)
        return exe

    wnd_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        n = user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value[: max(0, n)].strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append({"title": title, "exe": _exe_of(pid.value)})
        return True

    user32.EnumWindows(wnd_enum_proc(_enum_proc), 0)
    return windows


def pick_now_watching() -> dict | None:
    """选出最可能'用户正在看'的窗口，返回 parse_window_title 结果；无可感知窗口返回 None。

    排除：N.E.K.O 聊天客户端、开发终端、空标题/系统窗口。
    优先级：B站视频/直播 > 已知站点 > 网页 > 其他应用（游戏等）。
    """
    best = None
    best_score = -1
    try:
        windows = _visible_windows()
    except BiliError:
        return None
    for win in windows:
        parsed = parse_window_title(win["title"], win["exe"])
        if _skip_window(parsed):
            continue
        score = _window_score(parsed)
        if score > best_score:
            best_score, best = score, parsed
    return best


def now_watching(tab: dict | None = None) -> dict:
    """感知用户当前在看什么：扩展推送的活动标签页（精确）优先，否则枚举可见窗口。

    tab：浏览器扩展推送的 {url, title, at}。B站标签页按 URL 精确解析（bvid/room_id
    直接匹配详情，无需搜索）；非 B站标签页按标题通用解析。无扩展数据时回退窗口感知。
    """
    parsed: dict | None = None
    if tab and isinstance(tab, dict):
        url = str(tab.get("url") or "")
        title = str(tab.get("title") or "")
        parsed = parse_bili_url(url, title)
        if parsed is not None:
            parsed["from"] = "extension-tab"
        else:
            parsed = parse_window_title(title, "chrome.exe")
            parsed["from"] = "extension-tab"
    if parsed is None:
        parsed = pick_now_watching()
    if parsed is None:
        fg = _foreground_window()
        parsed = parse_window_title(fg["title"], fg["exe"])
    out = dict(parsed)
    # 精确匹配：扩展 tab 带 bvid → 直接取视频详情；live → 取直播间信息
    bvid = parsed.get("bvid")
    if bvid:
        try:
            info = video_info(bvid)
            out["video"] = {
                "bvid": bvid,
                "title": info["title"],
                "owner_name": info["owner_name"],
                "view": info["view"],
                "danmaku": info["danmaku"],
                "duration": _fmt_duration(info["duration"]),
                "pubdate": _fmt_time(info["pubdate"]),
                "desc": (info["desc"] or "")[:200],
            }
        except BiliError:
            out["video"] = None
    elif parsed.get("kind") == "video" and parsed.get("subject"):
        try:
            results = search(parsed["subject"], "video", limit=3)
        except BiliError:
            results = []
        if results:
            best = results[0]
            out["match"] = best
            try:
                info = video_info(best["bvid"])
                out["video"] = {
                    "bvid": best["bvid"],
                    "title": info["title"],
                    "owner_name": info["owner_name"],
                    "view": info["view"],
                    "danmaku": info["danmaku"],
                    "duration": _fmt_duration(info["duration"]),
                    "pubdate": _fmt_time(info["pubdate"]),
                    "desc": (info["desc"] or "")[:200],
                }
            except BiliError:
                out["video"] = None
    if parsed.get("kind") == "live":
        room_id = parsed.get("room_id")
        if room_id:
            try:
                info = live_info(room_id)
                out["live"] = {
                    "room_id": room_id,
                    "title": info.get("title") or "",
                    "uname": info.get("uname") or "",
                    "status_text": info.get("status_text") or "",
                    "online": info.get("online") or 0,
                    "area": info.get("area") or "",
                    "parent_area": info.get("parent_area") or "",
                }
            except BiliError:
                out["live"] = None
    return out


def format_now_watching(info: dict) -> str:
    kind = info.get("kind")
    if kind == "unknown":
        return "没有检测到前台窗口，无法感知当前内容。"
    if info.get("is_bili"):
        head = "用户正在看 B站" + ("直播" if kind == "live" else "视频") + "："
        if kind == "live" and info.get("live"):
            li = info["live"]
            lines = [
                head + f"「{li['title'] or info.get('subject') or '未知直播间'}」",
                f"主播：{li.get('uname') or '未知'} ｜ 状态：{li.get('status_text')} ｜ 在线：{li.get('online')} 人",
                f"分区：{li.get('parent_area') or ''} / {li.get('area') or ''} ｜ 房间号 {li.get('room_id')}",
            ]
            return "\n".join(lines)
        if kind == "live":
            return head + "「" + (info.get("subject") or "未知直播间") + "」（窗口：%s）" % info.get("window")
        if info.get("video"):
            v = info["video"]
            lines = [
                head + f"「{v['title']}」",
                f"UP主：{v['owner_name']} ｜ 播放 {v['view']} ｜ 弹幕 {v['danmaku']} ｜ 时长 {v['duration']}",
                f"发布时间：{v['pubdate']} ｜ BV号 {v['bvid']}",
            ]
            if v.get("desc"):
                lines.append("简介：" + v["desc"])
            return "\n".join(lines)
        if info.get("match"):
            m = info["match"]
            return head + f"「{m['title']}」（作者 {m['author']}）BV号 {m['bvid']}"
        return head + "「" + (info.get("subject") or "未知") + "」（未能精确匹配，可让用户提供视频链接）"
    app = info.get("app") or ""
    subject = info.get("subject") or ""
    if info.get("site"):
        return f"用户正在浏览 {info['site']}：{subject or '（首页/未知页面）'}（{app}）"
    if kind == "web":
        return f"用户正在浏览网页：{subject or '（未知页面）'}（{app}）"
    return f"用户正在使用应用：{app or '未知'}（窗口：{info.get('window') or '无标题'}）"
