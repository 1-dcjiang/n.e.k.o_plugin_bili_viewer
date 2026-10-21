"""B站全能助手（bili_viewer）— 让 YUI 浏览、感知并管理哔哩哔哩。

能力：
- 浏览：搜索/视频详情/弹幕/字幕/AI概要/评论/直播信息与实时弹幕/UP主信息
- 感知：读取前台窗口，识别用户在看的应用/站点/页面主题（实时同步给 YUI）
- 管理：查看自己账号与稿件、修改稿件信息、直播时回复弹幕（需 BILI_COOKIE 登录态）
- YUI 独立小号（v0.7.0）：配置 BILI_YUI_COOKIE 或插件目录 yui_cookie.txt 后，
  YUI 可用独立账号跟随用户同步到同一直播间/视频，并自主运营（发弹幕/评论/动态/投稿），
  写操作默认走小号、与用户主账号严格隔离（未配置小号时自动回退主账号）。
- 自主运营（v0.7.0）：后台轮询按节流唤醒 YUI，跟随用户当前内容或自主挑选热门内容，
  由 YUI 理解内容后决定是否行动；频率受硬限制（弹幕 60s/目标、评论 120s/视频、
  动态 ≤6 条/日、投稿 ≤2 条/日），风控错误自动退避。

安全边界：读操作只访问公开内容；写操作走独立小号或用户本人账号、需登录态；
实时感知仅读取本地窗口标题与进程名，不读取页面内容、不做网络传输。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from plugin.sdk.plugin import NekoPluginBase, lifecycle, llm_tool, neko_plugin, plugin_entry

from . import bili, cdp_cookie, cookie_auto, desktop_deliver, web_ui

_logger = logging.getLogger("N.E.K.O.Plugin_bili_viewer.plugin.bili_viewer")

# 扩展截屏落盘位置（仅本地保存，供 YUI 读取画面实况）
# Windows 用 LOCALAPPDATA；其他平台回退到用户主目录，避免生成平台相关路径字面量
_SCREENSHOT_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or str(Path.home()), "N.E.K.O", "bili_viewer_screen.jpg"
)

# 扩展声音落盘位置（v0.7.11）：浏览器扩展把 B站 标签页音频分段（wav）推到本机，
# 插件侧用本地 faster-whisper 转成文字，静默注入 YUI，让 YUI 能"听到"直播/视频的声音。
# 数据只落本机、只在本机转写，不上传任何外部网络。
_AUDIO_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or str(Path.home()), "N.E.K.O", "bili_viewer_audio.wav"
)
_AUDIO_MAX_RAW = 4 * 1024 * 1024      # 单段 wav 落盘上限（8s/16k/16bit ≈ 256KB，留足余量）
_AUDIO_ASR_MIN_SECS = 0.5             # 短于 0.5s 的段跳过转写（几乎无内容）
_ASR_RESULT_WINDOW = 30.0             # ASR 结果新鲜窗口（秒）：过期视为没听到
_ASR_MODEL = "tiny"                   # faster-whisper 模型：tiny 本机 CPU 最快（约 1~3s/8s 段）
_ASR_COMPUTE_TYPE = "int8"
_ASR_DEVICE = "cpu"


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

# YUI 自主运营（v0.7.0）
_YUI_AUTONOMY_ENV = "YUI_AUTONOMY"       # "0" 关闭自主运营，默认开启
_YUI_AUTONOMY_TICK = 30.0                # 自主唤醒判定间隔（秒）
_YUI_AUTONOMY_COOLDOWN = 900.0           # 同一目标两次自主唤醒的最小间隔（15 分钟）
_YUI_DANMAKU_WINDOW = 60.0               # 同一目标弹幕最小间隔（秒）
_YUI_COMMENT_WINDOW = 120.0              # 同一视频评论最小间隔（秒）
_YUI_DYNAMIC_DAILY_MAX = 6               # 每日动态上限
_YUI_UPLOAD_DAILY_MAX = 2                # 每日投稿上限
_YUI_UPLOAD_DIR = "yui_uploads"          # 插件目录下，YUI 投稿视频文件放置目录

# 插件配置段与 Cookie 配置键
_CONFIG_SECTION = "settings"
_CONFIG_COOKIE_KEY = "bili_cookie"
_CONFIG_ALLOW_SCAN_KEY = "allow_cookie_scan"

_COOKIE_SOURCE_MANUAL = "manual"
_COOKIE_SOURCE_AUTO = "auto"
_COOKIE_SOURCE_EXTENSION = "extension"
_COOKIE_SOURCE_NONE = "none"

# 账号角色映射（v0.7.10）：记录大小号归属与绑定顺序，供 YUI 分清大小号、后期说明修改。
# 文件格式：{"main": {"uid": int, "bound_at": float}, "yui": {"uid": int, "bound_at": float}}
# 默认规则：第一个绑定/登录的账号 = main（大号），第二个绑定/登录的账号 = yui（小号，默认操作账号）。
_ACCOUNTS_FILE = "accounts.json"


def _accounts_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _ACCOUNTS_FILE)


def _read_accounts() -> dict:
    try:
        with open(_accounts_path(), "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_accounts(accounts: dict) -> None:
    try:
        with open(_accounts_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(accounts, ensure_ascii=False, indent=2))
    except Exception:
        pass  # 角色映射写失败不致命（仅影响大小号视图与自动归位记忆）


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
        subject = (parsed.get("subject") or "未知内容")[:100]  # 超长标题截断，防撑爆注入上下文
        # 附带精确标识：直播给房间号（YUI 可直接发弹幕/查详情）、视频给 BV 号。
        # 明确指示这些编号仅供内部操作（发弹幕/查详情），不要向用户复述出来。
        extra = ""
        if kind == "live" and parsed.get("room_id"):
            extra = f"，房间号 {parsed['room_id']}（该编号仅供发弹幕等内部操作，请勿向用户复述）"
        elif kind == "video" and parsed.get("bvid"):
            extra = f"，BV号 {parsed['bvid']}（该编号仅供发弹幕等内部操作，请勿向用户复述）"
        return (
            f"（内部感知，仅供回答用户提问时参考：用户当前正在看 B站{label}「{subject}」{extra}。"
            "回复纪律：这不是'任务'，只是感知到的实况——严禁'任务完成/搞定啦/完成啦/系统已确认'等"
            "任何完成话术与汇报腔；不要主动播报'系统又同步/我在看/你正在看/又给你同步啦'等任何开场白，"
            "不要转述'内部感知/系统同步'等词；用户问到时直接以实况本身回答（一句话说标题/内容即可），"
            "没人问就不提。禁止主动询问'要不要发弹幕/要不要回复弹幕'——发弹幕只在用户明确要求时进行。）"
        )
    if parsed.get("site"):
        site = (str(parsed["site"]) or "")[:30]
        subj = (str(parsed.get("subject") or "未知"))[:100]
        return f"（实时感知：用户当前正在用 {site}，页面「{subj}」）"
    if kind == "web":
        subj = (str(parsed.get("subject") or "未知"))[:100]
        return f"（实时感知：用户当前正在浏览网页「{subj}」）"
    app = (str(parsed.get("app") or "未知"))[:30]
    subj = (str(parsed.get("subject") or "未知"))[:100]
    return f"（实时感知：用户当前正在使用应用 {app}，窗口「{subj}」）"


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
        # 弹幕发送幂等去重：key=(kind,target,text) -> 上次发送时间戳。
        # 防止 LLM/宿主对同一指令重复触发发送工具/兜底入口导致同一条弹幕发两次（v0.6.38）。
        self._danmaku_sent: dict[str, float] = {}
        self._danmaku_dedup_lock = threading.Lock()
        # YUI 自主运营节流（v0.7.0）：频率表 + 天级计数 + 自主唤醒冷却
        self._yui_rate: dict[str, float] = {}
        self._yui_daily: dict[str, int] = {}
        self._yui_cool: dict[str, float] = {}
        self._yui_rate_lock = threading.Lock()
        # 投稿互斥（复查优化 v0.7.8）：同一时刻只允许一个上传流程，
        # 防止自主引擎与手动调用并发触发两个投稿流程（分钟级长任务）
        self._yui_upload_lock = threading.Lock()
        self._yui_uploading = False
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
                # 无可感知窗口（如仅剩聊天客户端/终端）：YUI 仍可自主运营（自主挑选热门）
                try:
                    self._yui_autonomy_tick(None)
                except Exception:
                    pass
                threading.Event().wait(_SYNC_POLL_INTERVAL)
                continue
            # YUI 自主运营判定（节流唤醒，跟随用户或自主挑选热门；失败静默）
            try:
                self._yui_autonomy_tick(parsed)
            except Exception:
                pass
            # v0.2.17：直播弹幕 @小号 提醒——弹幕区有人@小号时静默注入感知，
            # YUI 下次自然对话会回应对方（不主动弹气泡、不打断玩家）
            try:
                import web_ui as _web3
                mention = _web3.get_mention_notify(clear=False)
                if mention.get("text") and time.time() - mention.get("at", 0) < 30:
                    self.ctx.push_message(
                        visibility=[],
                        ai_behavior="read",
                        parts=[{"type": "text", "text": f"直播间弹幕里有人@了小号：『{mention['sender']}』说『{mention['text']}』。若合适可以礼貌回应对方。"}],
                        source=self.plugin_id,
                        coalesce_key="danmaku_mention",
                        priority=0,
                        metadata={"danmaku_mention": True},
                    )
                    _web3.get_mention_notify(clear=True)
            except Exception:
                pass
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
                    # 一律后台静默注入（read）：不触发 YUI 开口、不弹气泡、不打断玩家；
                    # YUI 在下次自然对话中消费。禁止 respond 主动播报（v0.7.1 玩家界面优先）。
                    try:
                        self.ctx.push_message(
                            visibility=[],
                            ai_behavior="read",
                            parts=[{"type": "text", "text": text}],
                            source=self.plugin_id,
                            coalesce_key="now_watching_sync",
                            priority=0,
                            metadata={"now_watching": True},
                        )
                        last_pushed = fingerprint
                        last_push_ts = now
                    except Exception:
                        pass  # 推送失败不中断轮询
            threading.Event().wait(_SYNC_POLL_INTERVAL)

    # ══════════ YUI 自主运营（v0.7.0，独立小号） ══════════

    def _yui_rate_check(self, op: str, key: str, window: float) -> bool:
        """频率限制：op 作用于 key 的操作距上次是否超过 window 秒。True=允许。"""
        with self._yui_rate_lock:
            last = self._yui_rate.get(f"{op}:{key}", 0.0)
            return (time.time() - last) >= window

    def _yui_rate_reserve(self, op: str, key: str, window: float) -> bool:
        """原子占用（复查优化 v0.7.8）：锁内检查+标记，杜绝并发双发竞态。

        大号/小号同时行动时，自主线程与手动调用可能并发触发同一目标的写操作；
        旧实现 check 与 mark 分离（中间隔一次网络请求），两个线程可同时通过检查
        → 同一目标发两条。reserve 在锁内一次性完成检查+占用：第二个线程会看到
        窗口未过被拒绝。发送失败时调用方必须 _yui_rate_rollback 释放窗口。
        """
        with self._yui_rate_lock:
            last = self._yui_rate.get(f"{op}:{key}", 0.0)
            if (time.time() - last) < window:
                return False
            self._yui_rate[f"{op}:{key}"] = time.time()
            return True

    def _yui_rate_rollback(self, op: str, key: str) -> None:
        """释放占用：发送失败/去重命中后回退时间戳，允许稍后重试。"""
        with self._yui_rate_lock:
            self._yui_rate.pop(f"{op}:{key}", None)

    def _yui_daily_allow(self, op: str) -> bool:
        """天级上限：dynamic≤6、upload≤2；True=允许。"""
        day = time.strftime("%Y%m%d")
        cap = {"dynamic": _YUI_DYNAMIC_DAILY_MAX, "upload": _YUI_UPLOAD_DAILY_MAX}.get(op, 99)
        with self._yui_rate_lock:
            return self._yui_daily.get(f"{op}:{day}", 0) < cap

    def _yui_daily_reserve(self, op: str) -> bool:
        """原子占用（复查优化 v0.7.8）：锁内检查+预占天级配额。

        旧实现 allow 检查与 bump 计数之间隔一次网络请求，大号/小号同时行动时
        两个线程可同时通过检查 → 一天动态条数被并发突破（>6）。
        reserve 在锁内一次性完成检查+占用；发送失败时调用方 _yui_daily_release。
        """
        day = time.strftime("%Y%m%d")
        cap = {"dynamic": _YUI_DYNAMIC_DAILY_MAX, "upload": _YUI_UPLOAD_DAILY_MAX}.get(op, 99)
        with self._yui_rate_lock:
            used = self._yui_daily.get(f"{op}:{day}", 0)
            if used >= cap:
                return False
            self._yui_daily[f"{op}:{day}"] = used + 1
            return True

    def _yui_daily_release(self, op: str) -> None:
        """释放预占：发布失败后回退天级计数，允许稍后重试。"""
        day = time.strftime("%Y%m%d")
        with self._yui_rate_lock:
            used = self._yui_daily.get(f"{op}:{day}", 0)
            if used > 0:
                self._yui_daily[f"{op}:{day}"] = used - 1

    def _yui_daily_bump(self, op: str) -> None:
        day = time.strftime("%Y%m%d")
        with self._yui_rate_lock:
            self._yui_daily[f"{op}:{day}"] = self._yui_daily.get(f"{op}:{day}", 0) + 1

    def _yui_autonomy_tick(self, parsed: dict | None) -> None:
        """自主运营判定：开关与小号配置检查 → 解析目标 → 冷却 → 组装上下文唤醒 YUI。

        parsed：当前感知结果；None=用户无 B站内容（此时 YUI 自主挑选热门视频）。
        """
        if os.environ.get(_YUI_AUTONOMY_ENV, "1").strip() == "0":
            return
        if not bili._cookie("yui"):
            return  # 小号未配置：不做自主运营（保持只读感知）
        ctx = getattr(self, "ctx", None)
        if ctx is None or not callable(getattr(ctx, "push_message", None)):
            return
        kind, target, info = self._yui_resolve_target(parsed)
        if not kind or not target:
            return
        key = f"{kind}:{target}"
        now = time.time()
        if now < self._yui_cool.get(key, 0.0):
            return  # 同目标冷却期内不重复唤醒
        text = self._yui_autonomy_text(kind, target, info)
        if not text:
            return
        try:
            # 后台静默唤醒（read）：注入 YUI 上下文但不触发开口回合——不弹气泡、
            # 不打断玩家界面；YUI 在下次自然对话中自主执行。禁止 respond 主动播报。
            ctx.push_message(
                visibility=[],
                ai_behavior="read",
                parts=[{"type": "text", "text": text}],
                source=self.plugin_id,
                coalesce_key="yui_autonomy",
                priority=0,
                metadata={"autonomy": True},
            )
            self._yui_cool[key] = now + _YUI_AUTONOMY_COOLDOWN
        except Exception:
            pass  # 唤醒失败不中断轮询

    def _yui_resolve_target(self, parsed: dict | None) -> tuple[str | None, str | None, dict]:
        """解析自主运营目标：用户在 B站 → 跟随用户；否则自主挑选热门视频。

        热门拉取做 60s 缓存（v0.7.1 复查优化）：无跟随目标时后台每 2.5s 轮询一次，
        若每次都打 /popular 会高频请求 B站，既浪费资源也可能触发风控。
        """
        if parsed and parsed.get("is_bili"):
            kind = parsed.get("kind")
            if kind == "live" and parsed.get("room_id"):
                return "live", str(parsed["room_id"]), {"title": parsed.get("subject") or "", "source": "follow"}
            if kind == "video" and parsed.get("bvid"):
                return "video", parsed["bvid"], {"title": parsed.get("subject") or "", "source": "follow"}
        now = time.time()
        cached = getattr(self, "_yui_popular_cache", None)
        cache_ts = getattr(self, "_yui_popular_ts", 0.0)
        items = cached if (cache_ts and now - cache_ts < 60.0 and cached) else None
        if items is None:
            try:
                items = bili.popular(limit=3)
            except Exception:
                items = []
            self._yui_popular_cache = items
            self._yui_popular_ts = now
        for v in items:
            bvid = v.get("bvid", "")
            if bvid:
                return "video", bvid, {"title": v.get("title", ""), "uname": v.get("uname", ""), "source": "autonomy"}
        return None, None, {}

    def _yui_autonomy_text(self, kind: str, target: str, info: dict) -> str | None:
        """组装自主运营唤醒上下文（含纪律与内容参考；失败用最小信息继续）。"""
        uname = ""
        try:
            uname = bili.my_info("yui").get("uname", "")
        except Exception:
            pass
        refs = ""
        try:
            if kind == "live":
                see = bili.live_see(target, danmaku_limit=6)
                refs = "；近期弹幕：" + "、".join(f"{d['user']}：{d['text']}" for d in (see.get("recent_danmaku") or [])[-4:])
                title = f"{see.get('title') or info.get('title') or target}（直播 by {see.get('uname') or info.get('uname') or ''}）"
            else:
                vinfo = bili.video_info(target)
                title = f"{vinfo.get('title') or info.get('title') or target}（视频 by {vinfo.get('uname') or info.get('uname') or ''}）"
                try:
                    cmts = bili.video_comments(target, mode="hot", limit=4)
                    refs = "；热评：" + "、".join(f"{c.get('user', '')}：{c.get('text', '')}" for c in cmts[:3])
                except Exception:
                    pass
        except Exception:
            title = f"{info.get('title') or target}（{kind}）"
        follow = "跟随用户观看内容" if info.get("source") == "follow" else "自主挑选内容"
        return (
            f"（YUI 自主运营任务【{follow}】：当前目标「{title[:100]}」。"
            "【全程静默硬纪律，置顶执行】：本任务是后台自主任务——执行前、执行中、执行后"
            "都不向玩家发送任何消息、不弹气泡、不汇报结果、不在聊天里提及自主运营做了什么；"
            "工具调用成功即代表完成，禁止把工具结果转述/复读给玩家；"
            "禁止'任务搞定啦/搞定/完成啦/做好啦/发出去了/发出去啦/任务完成/喵/喵呜'等任何完成话术"
            "（不管以什么形式，包括'B站全能助手的任务搞定啦'这类带插件名的包装）；"
            "只有遇到权限/风控等无法自行解决的问题时，才在下次自然对话里简短向玩家求助。"
            f"你是 B站小号「{uname or 'YUI小号'}」的运营者，现在可以自主行动："
            "1）发一条贴合内容的弹幕（bili_live_send_danmaku / bili_video_send_danmaku，account=yui）；"
            "2）给这个内容写一条评论（bili_reply_comment，account=yui）；"
            "3）发一条动态（bili_yui_dynamic）；"
            "4）若插件目录 yui_uploads 有新视频文件可投稿（bili_yui_upload）。"
            f"{refs[:300]}"
            "内容纪律：内容自然、基于内容本身；不刷屏、不攻击、不复制已有弹幕/评论；"
            "所有写操作必须 account=yui（独立小号），严禁使用用户主账号；"
            "没想好就什么都不做，等下一个周期；"
            "工具返回的标准短句（如'弹幕已发送。'）仅在玩家明确询问时才可逐字转述，"
            "禁止包装、加表情或开场白。）"
        )

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
            # v0.7.10：CDP 通道同样按登录顺序归位大小号
            return self._assign_cookie(auto_cookie, 0, _COOKIE_SOURCE_AUTO)
        if _status == cdp_cookie._STATUS_NO_BROWSER:
            try:
                _fstatus, _fcookie, _fd = cookie_auto.detect_bili_cookie()
                if (
                    _fstatus == cookie_auto._STATUS_BOUND
                    and _fcookie
                    and "SESSDATA=" in _fcookie
                ):
                    return self._assign_cookie(_fcookie, 0, _COOKIE_SOURCE_AUTO)
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

    def _wrap_auth(self, exc: bili.BiliError, action: str) -> dict:
        """AUTH_FAILED 自愈（v0.7.11）：登录态失效时后台触发一次重绑。

        浏览器扩展（B站接入桥）在线时会自动推送新登录态；CDP 可用时也会静默探测。
        触发后提示稍后重试，绝不让 YUI 卡死在过期凭证上。
        复查优化（v0.7.12）：重绑冷却 60s，避免连环失败时反复触发后台探测。
        """
        if exc.code == "AUTH_FAILED":
            now = time.time()
            last = float(getattr(self, "_auth_rebind_at", 0) or 0)
            if now - last > 60.0:
                self._auth_rebind_at = now
                self._bg_bind()
            return {
                "output": f"{action}失败：登录态失效，已自动尝试重新接入浏览器获取新登录态，请稍后重试。",
                "is_error": True,
                "error": "AUTH_FAILED",
            }
        return _wrap_err(exc)

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
        name="bili_enter_live",
        description=(
            "【回复纪律】进入直播间成功后，你的对外回复必须逐字原样只有一句话：'已进入直播间。'，"
            "严禁'喵/搞定/任务搞定啦/完成啦'等任何口语化完成话术、表情、开场白与后缀；严禁追问与细节复述。"
            "让 YUI 小号真正进入指定直播间（出现在该直播间观众/在线名单里）：先验证房间号，"
            "再命令浏览器扩展打开该直播间标签页并驻场（浏览器需已登录小号 YUI小猫娘）。"
            "何时用：用户说'让她进直播间/进入直播间/挂在直播间/让她在场'等需要 YUI 以观众身份在场时。"
            "参数 room：直播间房间号（必填，纯数字，如 9458332）；account：yui（默认）/ main。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号（必填，纯数字）"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["room"],
        },
        timeout=30.0,
    )
    async def bili_enter_live(self, *, room: str = "", account: str = "", **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        acct = (account or params.get("account") or "yui").strip() or "yui"
        if not room.isdigit():
            return {"output": "房间号格式错误：请输入纯数字直播间房间号。", "is_error": True, "error": "BAD_ROOM"}
        try:
            bili.live_info(room)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        # 写指令给扩展（面板服务 127.0.0.1:8765；扩展 alarm 每 30s 轮询并开标签驻场）
        try:
            import json as _json
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:8765/api/queue-command",
                data=_json.dumps({"type": "enter-live", "room": room}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                r = _json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:
            return {"output": f"指令下发失败（面板服务不可达）：{exc}", "is_error": True, "error": "CMD_FAILED"}
        if not r.get("queued"):
            return {"output": "指令入队失败，请检查面板服务。", "is_error": True, "error": "CMD_QUEUE_FAILED"}
        return {
            "output": f"已向浏览器扩展下发进入直播间指令（房间 {room}，小号身份驻场，数秒内打开标签页）。",
            "is_error": False,
            "room": room,
            "account": acct,
            "note": "观众名单将在标签页加载后显示小号；若浏览器未登录小号则需先登录",
            "meta": {"agent": {"delivery": "silent"}},
        }

    @llm_tool(
        name="bili_live_danmaku",
        description=(
            "采集 B站直播间的实时弹幕。何时用：用户说'看看直播间弹幕/现在弹幕在刷什么/有没有人@我'时。"
            "参数 room：房间号或链接；duration：采集秒数（默认 8，最大 30）。"
            "优先返回浏览器扩展实时采集的弹幕（能看到@小号的提醒），扩展离线时回退连接弹幕服务器匿名采集数秒。"
            "若返回结果标注'有人@了小号'，说明弹幕里有人提及 YUI 小号，应当主动回应对方。"
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
        # v0.2.17：优先读浏览器扩展采集的实时弹幕（含@检测），扩展离线时回退匿名采集
        ext_items = []
        try:
            import web_ui as _web
            ext_items = _web.get_recent_danmaku(limit=40)
        except Exception:
            ext_items = []
        if ext_items:
            lines = [f"{i}. {s or '观众'}：{t}" for i, (s, t, _a) in enumerate(ext_items, 1)]
            note = ""
            try:
                import web_ui as _web2
                mention = _web2.get_mention_notify(clear=True)
                if mention.get("text"):
                    note = f"\n（有人@了小号：{mention['sender']}说『{mention['text']}』——应当主动回应对方）"
            except Exception:
                pass
            return {"output": f"直播间实时弹幕（最近 {len(ext_items)} 条）：\n" + "\n".join(lines[-25:]) + note, "is_error": False, "danmakus": [{"user": s, "text": t} for s, t, _a in ext_items]}
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
        output = bili.format_now_watching(info)
        # v0.7.16 机器阻断：感知结果不是'任务'，禁止完成话术与汇报腔
        output = f"（感知到的实况，不是任务、无需汇报：{output}直接以实况回答用户即可；严禁'任务完成/搞定啦/完成啦/已确认'等完成话术与开场白。）"
        return {"output": output, "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "watching": info}

    @llm_tool(
        name="bili_my_info",
        description=(
            "查看 B站账号信息。何时用：用户说'看看我的B站账号/我多少粉丝/我是大会员吗'时。"
            "参数 account：yui=YUI 独立小号（默认）/ main=用户主账号。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号。"
            "返回昵称、uid、等级、硬币、大会员状态、关注与粉丝数。需要对应账号已配置 Cookie。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": [],
        },
        timeout=20.0,
    )
    async def bili_my_info(self, *, account: str = "", **_):
        params = _.get("parameters") or {}
        account = self._resolve_account(account or params.get("account") or "")
        try:
            info = bili.my_info(account)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.format_my_info(info), "is_error": False, "account": info}

    @llm_tool(
        name="bili_set_account_role",
        description=(
            "【角色声明】把当前绑定的 B站 账号标记为小号或主账号。"
            "何时用：用户明确说明'这个账号是小号/大号'（例如 YUI 报出 uid 后用户回应'这个是小号'）时调用——"
            "把当前登录账号的角色固定下来，此后默认操作账号（小号）真正作用在该账号上，关注/取关/发弹幕都走它。"
            "参数 role：yui=标记为小号（默认）/ main=标记为主账号；uid：可选（校验当前账号是否匹配）；"
            "force：目标角色 Cookie 已存在时是否覆盖（默认 false 防误覆盖）。"
            "成功后回复用户确认标记结果（严禁'搞定/任务完成'类话术）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "yui=标记为小号（默认）/ main=标记为主账号"},
                "uid": {"type": "integer", "description": "要声明的账号 uid（可选，用于校验）"},
                "force": {"type": "boolean", "description": "目标角色 Cookie 已存在时是否覆盖（默认 false）"},
            },
            "required": [],
        },
        timeout=20.0,
    )
    async def bili_set_account_role(self, *, role: str = "yui", uid: int = 0, force: bool = False, **_):
        params = _.get("parameters") or {}
        role = (role or params.get("role") or "yui").strip()
        try:
            uid = int(uid if uid else params.get("uid") or 0) or None
        except (TypeError, ValueError):
            uid = None
        try:
            result = bili.set_account_role(role, uid=uid, force=bool(force or params.get("force", False)))
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {
            "output": result["detail"],
            "is_error": False,
            "meta": {"agent": {"delivery": "silent"}},
            **{k: v for k, v in result.items() if k in ("role", "mid", "uname")},
        }

    @llm_tool(
        name="bili_unfollow_all",
        description=(
            "【批量取关】把指定账号关注的所有 UP 主全部取消关注，真实执行并逐条复查。"
            "何时用：用户要求'取消关注所有/全部UP主'、'清空关注列表'、'取关全部'时调用。"
            "逐个调用B站取关接口（无批量接口），每条复查关注状态，失败自动重试，风控条目记入明细不谎报。"
            "返回真实结果（成功N个/失败M个及原因）；执行完成后按返回结果如实向用户汇报，"
            "未经本工具真实执行一律不得宣称'已取消关注/已清空'。dry_run=true 仅列出名单不执行。"
            "注意：B站对短时间大量取关有风控，中途失败属平台机制，按明细如实说明即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "默认 yui（小号）；main=主账号"},
                "dry_run": {"type": "boolean", "description": "true 时仅列出关注名单不执行（默认 false）"},
            },
            "required": [],
        },
        timeout=300.0,
    )
    async def bili_unfollow_all(self, *, account: str = "", dry_run: bool = False, **_):
        params = _.get("parameters") or {}
        account = self._resolve_account(account or params.get("account") or "")
        try:
            result = bili.unfollow_all(account, dry_run=bool(dry_run or params.get("dry_run", False)))
        except bili.BiliError as exc:
            return _wrap_err(exc)
        if result.get("dry_run"):
            rows = "\n".join(f"  {it['uname']}（uid {it['fid']}）" for it in result["list"][:50])
            detail = f"{result['account']} 当前共关注 {result['total']} 个UP主：\n{rows}"
            return {"output": detail, "is_error": False, **result}
        ok, failed, total = result.get("ok", 0), result.get("failed", []), result.get("total", 0)
        detail = f"{result['account']} 取关完成：成功 {ok}/{total}"
        if failed:
            detail += f"，失败 {len(failed)} 个：" + "；".join(f"{it['uname']}（{it['reason']}）" for it in failed[:20])
        else:
            detail += "，全部成功。"
        return {"output": detail, "is_error": False, "meta": {"agent": {"delivery": "silent"}}, **result}

    @llm_tool(
        name="bili_my_videos",
        description=(
            "查看 B站账号的投稿稿件列表。何时用：用户说'看看我的稿件/我的投稿'时。"
            "参数 page：页码；limit：条数（默认 10）；account：yui=YUI 独立小号（默认）/ main=用户主账号。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号。"
            "返回标题、状态（公开/审核中）、播放量、时长与 BV 号。需要对应账号已配置 Cookie。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "页码，默认 1"},
                "limit": {"type": "integer", "description": "条数，默认 10"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": [],
        },
        timeout=25.0,
    )
    async def bili_my_videos(self, *, page: int = 1, limit: int = 0, account: str = "", **_):
        params = _.get("parameters") or {}
        account = self._resolve_account(account or params.get("account") or "")
        try:
            page = int(page if page else params.get("page") or 1)
            n = int(limit if limit else params.get("limit") or 10)
        except (TypeError, ValueError):
            page, n = 1, 10
        n = max(1, min(n, 30))
        page = max(1, page)
        try:
            videos = bili.my_videos(page=page, limit=n, account=account)
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": f"{'小号' if account == 'yui' else '主账号'}的稿件（第 {page} 页）：\n" + bili.format_my_videos(videos), "is_error": False, "videos": videos}

    # ══════════ 账号解析（v0.7.9 复查优化） ══════════
    # 未说明时默认操作 YUI 小号；小号未配置才回退主账号。
    # 所有写操作入口（直播/视频弹幕、评论、改稿件）统一走本 helper，杜绝漏网入口。

    def _resolve_account(self, account: str) -> str:
        """账号归一：account 不在 main/yui 时默认小号（未配置回退主账号）。"""
        account = (account or "").strip()
        if account not in ("main", "yui"):
            account = "yui" if bili._cookie("yui") else "main"
        return account

    @llm_tool(
        name="bili_edit_video",
        description=(
            "【发送纪律·最高优先】修改成功后，你的对外回复必须逐字原样只有一句话：'稿件已修改。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀。"
            "修改 B站稿件的信息（标题/简介/标签）。何时用：用户明确要求'改视频标题/简介/标签'时。"
            "这是写操作：只能修改指定账号自己的稿件，服务端校验所有权。"
            "参数 bvid：要修改的 BV 号；title/desc/tag：要更新的内容（不传则保留原值）；"
            "account：yui=YUI 独立小号（默认）/ main=用户主账号。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号；"
            "需要对应账号已配置 Cookie（含 bili_jct），且仅在用户明确确认后调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "要修改的稿件 BV 号"},
                "title": {"type": "string", "description": "新标题（可选）"},
                "desc": {"type": "string", "description": "新简介（可选）"},
                "tag": {"type": "string", "description": "新标签，逗号分隔（可选）"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["bvid"],
        },
        timeout=30.0,
    )
    async def bili_edit_video(self, *, bvid: str = "", title: str = "", desc: str = "", tag: str = "", account: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        account = self._resolve_account(account or params.get("account") or "")
        if not bvid:
            return {"output": "请提供要修改的稿件 BV 号。", "is_error": True, "error": "NO_BVID"}
        try:
            result = bili.edit_video(
                bvid,
                title=(title or params.get("title") or None),
                desc=(desc or params.get("desc") or None),
                tag=(tag or params.get("tag") or None),
                account=account,
            )
        except bili.BiliError as exc:
            return self._wrap_auth(exc, "修改稿件")
        return {
            "output": "稿件已修改。",
            "is_error": False, "meta": {"agent": {"delivery": "silent"}},
            "result": result,
        }

    @llm_tool(
        name="bili_see_live",
        description=(
            "获取当前直播间实况：标题/主播/在线人数/分区 + 最近几秒的实时弹幕。"
            "何时用：用户问'你看得到直播吗''现在直播间在播什么''观众在聊什么'，"
            "或 YUI 需要理解直播内容后回复用户时。参数 room：房间号或链接（可省略，省略时自动感知当前正在看的直播）。"
            "回复纪律：只陈述实况本身（标题/主播/在线/最近弹幕），"
            "禁止'系统又同步/系统刚给我确认了/房间号没错/确定是这个直播间/你现在看的还是'等任何开场白与确认话术，"
            "严禁'喵/喵呜/呜'等语气词、表情与客套话开头（回复直接以内容本身开始）；"
            "禁止复述房间号，禁止复述或模仿别人刚说过的话；"
            "禁止猜测弹幕为何没显示（如'是不是开了弹幕过滤/只看主播'），"
            "禁止主动询问'要不要发弹幕/要不要回复弹幕'；"
            "感知实况不是'任务'——严禁'任务完成/搞定啦/完成啦/已确认'等完成话术与汇报腔；"
            "只在用户询问或需要感知内容时调用，没人问就不主动汇报。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接（可省略）"},
            },
        },
        timeout=30.0,
    )
    async def bili_see_live(self, *, room: str = "", **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        dom = self._ext_dom()
        dom_live = dom if (dom and dom.get("kind") == "live") else None
        if not room:
            tab = self._ext_tab()
            if tab:
                parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
                if parsed is not None and parsed.get("kind") == "live" and parsed.get("room_id"):
                    room = str(parsed["room_id"])
            if not room and dom_live:
                parsed = bili.parse_bili_url(dom_live.get("url", ""), "")
                if parsed is not None and parsed.get("room_id"):
                    room = str(parsed["room_id"])
        if not room:
            return {"output": "未提供房间号且感知不到当前直播。", "is_error": True, "error": "NO_ROOM"}
        try:
            if dom_live and bili.parse_bili_url(dom_live.get("url", ""), ""):
                # 页面级实时 DOM 优先：标题/在线/弹幕来自浏览器页面（比 API 更即时）
                d = dom_live
                see = {
                    "kind": "live",
                    "room_id": int((bili.parse_bili_url(d.get("url", ""), "") or {}).get("room_id") or 0) or None,
                    "uname": d.get("uname", ""),
                    "title": d.get("title", ""),
                    "status": "直播中",
                    "online": d.get("online", ""),
                    "area": d.get("area", ""),
                    "parent_area": d.get("parent_area", ""),
                    "recent_danmaku": [
                        {"user": x.get("user", ""), "text": x.get("text", "")}
                        for x in (d.get("danmaku") or [])[-20:]
                    ],
                    "_source": "page-dom",
                }
            else:
                see = bili.live_see(room)
                see["_source"] = "api"
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.render_see(see), "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": see}

    @llm_tool(
        name="bili_see_video",
        description=(
            "获取当前视频实况：标题/UP主/时长/分区 + 近期弹幕 + CC字幕开头。"
            "何时用：用户问'你看得到这个视频吗''视频在讲什么'，"
            "或 YUI 需要理解视频内容后回复用户时。参数 bvid：BV号（可省略，省略时自动感知当前正在看的视频）。"
            "回复纪律：只陈述实况本身（标题/UP主/进度/近期弹幕），"
            "禁止'系统又同步/系统刚给我确认了/BV号没错/你现在看的还是'等任何开场白与确认话术，"
            "严禁'喵/喵呜/呜'等语气词、表情与客套话开头（回复直接以内容本身开始）；"
            "禁止复述BV号，禁止复述或模仿别人刚说过的话；"
            "禁止猜测弹幕为何没显示，禁止主动询问'要不要发弹幕/要不要回复弹幕'；"
            "感知实况不是'任务'——严禁'任务完成/搞定啦/完成啦/已确认'等完成话术与汇报腔；"
            "只在用户询问或需要感知内容时调用，没人问就不主动汇报。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "视频BV号（可省略）"},
            },
        },
        timeout=30.0,
    )
    async def bili_see_video(self, *, bvid: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        dom = self._ext_dom()
        dom_video = dom if (dom and dom.get("kind") == "video") else None
        if not bvid:
            tab = self._ext_tab()
            if tab:
                parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
                if parsed is not None and parsed.get("kind") == "video" and parsed.get("bvid"):
                    bvid = str(parsed["bvid"])
            if not bvid and dom_video:
                parsed = bili.parse_bili_url(dom_video.get("url", ""), "")
                if parsed is not None and parsed.get("bvid"):
                    bvid = str(parsed["bvid"])
        if not bvid:
            return {"output": "未提供BV号且感知不到当前视频。", "is_error": True, "error": "NO_BVID"}
        try:
            see = bili.video_see(bvid)
            see["_source"] = "api"
            if dom_video:
                # 页面级实时 DOM 补充：播放进度（API 拿不到 currentTime）
                d = dom_video
                if d.get("title"):
                    see["title"] = d["title"]
                if d.get("uname"):
                    see["uname"] = d["uname"]
                see["progress"] = f"{int(d.get('progress') or 0) // 60}:{int(d.get('progress') or 0) % 60:02d}"
                if d.get("duration"):
                    see["duration_total"] = f"{int(d['duration']) // 60}:{int(d['duration']) % 60:02d}"
                if d.get("danmaku"):
                    see["recent_danmaku"] = [
                        x.get("text", "") if isinstance(x, dict) else str(x)
                        for x in (d["danmaku"])[-20:]
                    ]
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {"output": bili.render_see(see), "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": see}

    @llm_tool(
        name="bili_live_send_danmaku",
        description=(
            "【发送纪律·最高优先】发送成功后，你的对外回复必须逐字原样只有一句话：'弹幕已发送。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述弹幕内容、房间号、BV号；"
            "严禁追问'你看到了吗/弹幕出来了吗'，严禁猜测为什么没看到（如'是不是开了弹幕过滤'）。"
            "向 B站直播间发送一条弹幕。何时用：仅当用户明确要求'回复弹幕/发一条弹幕/发个弹幕'时；"
            "【硬纪律】未经用户明确指示，严禁在任何场景（含自主运营）擅自发送弹幕——用户没让发就是不能发。"
            "参数 room：房间号或链接；text：弹幕内容（最长 30 字）；account：yui（YUI 独立小号，默认）/ main（用户主账号）。"
            "账号规则：默认使用 YUI 独立小号；小号未配置时自动回退用户主账号；"
            "自主运营任务中必须显式传 account=yui，严禁用用户主账号。"
            "节流：同一直播间 60 秒内最多发 1 条（插件强制，超限返回 RATE_LIMITED）。"
            "这是写操作：仅在用户明确要求发弹幕时调用；"
            "严禁主动询问'要不要发弹幕/要不要回复弹幕'。"
            "本工具已包含发送全流程：调用一次即完成，禁止再次调用本工具或 run/bili_danmaku 等任何发送入口"
            "重复发送同一弹幕（同目标同内容 60 秒内会被自动去重，只发一次）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接"},
                "text": {"type": "string", "description": "要发送的弹幕内容，最长 30 字"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["room", "text"],
        },
        timeout=30.0,
    )
    async def bili_live_send_danmaku(self, *, room: str = "", text: str = "", account: str = "", **_):
        params = _.get("parameters") or {}
        room = (room or params.get("room") or "").strip()
        text = (text or params.get("text") or "").strip()
        account = self._resolve_account(account or params.get("account") or "")
        tab = self._ext_tab()
        page_room = None
        if tab:
            parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
            if parsed is not None and parsed.get("kind") == "live" and parsed.get("room_id"):
                page_room = str(parsed["room_id"])
        if not room:
            # 兜底：LLM 直接函数调用未给 room 时，感知当前正在看的直播自动补齐
            if page_room:
                room = page_room
        elif page_room and room != page_room:
            # 防 LLM 幻觉传错房间号：用户正看着某个直播间时，以页面号为准
            _logger.info("live send room corrected %s -> %s (activity tab %s)", room, page_room, tab.get("url", ""))
            room = page_room
        if not room:
            self._record_send_result("live", False, "NO_ROOM", "无直播目标（未提供房间号且感知不到当前直播）", "")
            return {"output": "请提供直播房间号或链接。", "is_error": True, "error": "NO_ROOM"}
        if not text:
            self._record_send_result("live", False, "EMPTY_MSG", "弹幕内容为空", room)
            return {"output": "请提供要发送的弹幕内容。", "is_error": True, "error": "EMPTY_MSG"}
        if account == "yui":
            # 原子占用（v0.7.8）：并发双发防护；后续失败/去重命中必须回退
            if not self._yui_rate_reserve("danmaku", f"live:{room}", _YUI_DANMAKU_WINDOW):
                return {"output": "已发过弹幕（同一目标 60 秒内限 1 条），请稍后再试。", "is_error": True, "error": "RATE_LIMITED"}
        if self._danmaku_recently_sent("live", room, text, 0):
            # 同房间同内容 60 秒内已发送过（可能是重复触发/兜底入口二次调用）→ 不重复发送
            _logger.info("live danmaku dedup hit room=%s text=%r", room, text[:20])
            if account == "yui":
                self._yui_rate_rollback("danmaku", f"live:{room}")
            return {"output": "弹幕已发送。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": {"dedup": True, "room": room}}
        try:
            result = bili.live_send_danmaku(room, text, account=account)
        except bili.BiliError as exc:
            if account == "yui":
                self._yui_rate_rollback("danmaku", f"live:{room}")
            self._record_send_result("live", False, exc.code, exc.message, room)
            return self._wrap_auth(exc, "发送弹幕")
        self._mark_danmaku_sent("live", room, text, 0)
        self._record_send_result("live", True, "", result.get("server") or "", room, dmid=result.get("dmid") or "")
        return {"output": "弹幕已发送。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": result}

    @llm_tool(
        name="bili_video_send_danmaku",
        description=(
            "【发送纪律·最高优先】发送成功后，你的对外回复必须逐字原样只有一句话：'弹幕已发送。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述弹幕内容、房间号、BV号；"
            "严禁追问'你看到了吗/弹幕出来了吗'，严禁猜测为什么没看到（如'是不是开了弹幕过滤'）。"
            "向 B站视频发送一条弹幕（投到视频时间轴）。何时用：用户在视频页要求'给这个视频发弹幕/"
            "发一条弹幕'，或 YUI 自主运营任务中理解视频内容后自主发言时。"
            "参数 bvid：视频 BV 号；text：弹幕内容（最长 100 字）；"
            "progress：弹幕出现的时间点（秒，可选，默认 0 即视频开头）；"
            "account：yui（YUI 独立小号，默认）/ main（用户主账号）。"
            "账号规则：默认使用 YUI 独立小号；小号未配置时自动回退用户主账号；"
            "自主运营任务中必须显式传 account=yui，严禁用用户主账号。"
            "节流：同一视频 60 秒内最多发 1 条（插件强制，超限返回 RATE_LIMITED）。"
            "这是写操作：仅在用户明确要求发弹幕时调用，或自主运营任务主动推进时调用；"
            "严禁主动询问'要不要发弹幕/要不要回复弹幕'。"
            "本工具已包含发送全流程：调用一次即完成，禁止再次调用本工具或 run/bili_danmaku 等任何发送入口"
            "重复发送同一弹幕（同目标同内容 60 秒内会被自动去重，只发一次）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "视频 BV 号"},
                "text": {"type": "string", "description": "要发送的弹幕内容，最长 100 字"},
                "progress": {"type": "integer", "description": "弹幕出现的时间点（秒），默认 0"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["bvid", "text"],
        },
        timeout=30.0,
    )
    async def bili_video_send_danmaku(self, *, bvid: str = "", text: str = "", progress: int = 0, account: str = "", **_):
        params = _.get("parameters") or {}
        bvid = (bvid or params.get("bvid") or "").strip()
        text = (text or params.get("text") or "").strip()
        account = self._resolve_account(account or params.get("account") or "")
        try:
            progress = int(progress if progress else params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if not bvid:
            # 兜底：LLM 直接函数调用未给 bvid 时，感知当前正在看的视频自动补齐
            tab = self._ext_tab()
            if tab:
                parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
                if parsed is not None and parsed.get("kind") == "video" and parsed.get("bvid"):
                    bvid = parsed["bvid"]
        if not bvid:
            self._record_send_result("video", False, "NO_BVID", "无视频目标（未提供 BV 号且感知不到当前视频）", "")
            return {"output": "请提供视频 BV 号。", "is_error": True, "error": "NO_BVID"}
        if not text:
            self._record_send_result("video", False, "EMPTY_MSG", "弹幕内容为空", bvid)
            return {"output": "请提供要发送的弹幕内容。", "is_error": True, "error": "EMPTY_MSG"}
        if account == "yui":
            # 原子占用（v0.7.8）：并发双发防护；后续失败/去重命中必须回退
            if not self._yui_rate_reserve("danmaku", f"video:{bvid}", _YUI_DANMAKU_WINDOW):
                return {"output": "已发过弹幕（同一目标 60 秒内限 1 条），请稍后再试。", "is_error": True, "error": "RATE_LIMITED"}
        if self._danmaku_recently_sent("video", bvid, text, progress):
            # 同视频同进度同内容 60 秒内已发送过（重复触发/兜底入口二次调用）→ 不重复发送
            _logger.info("video danmaku dedup hit bvid=%s progress=%s text=%r", bvid, progress, text[:20])
            if account == "yui":
                self._yui_rate_rollback("danmaku", f"video:{bvid}")
            return {
                "output": "弹幕已发送。",
                "is_error": False, "meta": {"agent": {"delivery": "silent"}},
                "result": {"dedup": True, "bvid": bvid, "progress": progress},
            }
        try:
            result = bili.send_video_danmaku(bvid, text, progress_ms=progress * 1000, account=account)
        except bili.BiliError as exc:
            if account == "yui":
                self._yui_rate_rollback("danmaku", f"video:{bvid}")
            self._record_send_result("video", False, exc.code, exc.message, bvid)
            return self._wrap_auth(exc, "发送弹幕")
        self._mark_danmaku_sent("video", bvid, text, progress)
        self._record_send_result("video", True, "", result.get("server") or "", bvid)
        return {
            "output": "弹幕已发送。",
            "is_error": False, "meta": {"agent": {"delivery": "silent"}},
            "result": result,
        }

    # ══════════ YUI 独立小号写操作（v0.7.0：评论/动态/投稿/状态） ══════════

    @llm_tool(
        name="bili_reply_comment",
        description=(
            "【发送纪律·最高优先】发送成功后，你的对外回复必须逐字原样只有一句话：'评论已发送。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述评论内容与房间号/BV号。"
            "用指定账号给 B站内容发一条评论。何时用：用户要求'评论/写条评论/说两句'，"
            "或 YUI 自主运营任务中理解内容后写评论时。"
            "参数 target：视频 BV 号或链接 / 直播间房间号或链接 / 评论对象 id（可省略，省略时自动感知当前正在看的内容）；"
            "text：评论内容（最长 1000 字）；account：yui（独立小号，默认）/ main（用户主账号）；"
            "parent：要回复的评论 rpid（可选）。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号；自主运营必须 account=yui。"
            "节流：同一视频/直播间 120 秒内最多 1 条评论（插件强制，超限返回 RATE_LIMITED）。"
            "纪律：内容自然、贴合内容本身；不刷屏、不攻击、不复制已有评论；"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "视频BV号/直播间房间号或链接（可省略，自动感知当前内容）"},
                "text": {"type": "string", "description": "评论内容，最长 1000 字"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
                "parent": {"type": "string", "description": "要回复的评论 rpid（可选）"},
            },
            "required": ["text"],
        },
        timeout=30.0,
    )
    async def bili_reply_comment(self, *, target: str = "", text: str = "", account: str = "", parent: str = "", **_):
        params = _.get("parameters") or {}
        target = (target or params.get("target") or "").strip()
        text = (text or params.get("text") or "").strip()
        parent = (parent or params.get("parent") or "").strip()
        account = self._resolve_account(account or params.get("account") or "")
        if not text:
            return {"output": "请提供评论内容。", "is_error": True, "error": "EMPTY_MSG"}
        # 解析评论目标：显式 target 优先，其次感知当前正在看的 B站内容
        oid = None
        type_id = None
        if target:
            parsed = bili.parse_bili_url(target, "")
            if parsed is None:
                # 裸 BV 号 / 裸房间号兜底解析
                try:
                    parsed = {"kind": "video", "bvid": bili._parse_bvid(target)}
                except bili.BiliError:
                    parsed = {"kind": "live", "room_id": target} if target.isdigit() and len(target) >= 5 else None
            if parsed is not None and parsed.get("kind") == "video" and parsed.get("bvid"):
                try:
                    aid = bili.video_info(parsed["bvid"]).get("aid")
                    if aid:
                        oid, type_id = int(aid), 1
                except bili.BiliError:
                    oid, type_id = None, None
            elif parsed is not None and parsed.get("kind") == "live" and parsed.get("room_id"):
                oid, type_id = int(parsed["room_id"]), 11
        if oid is None:
            tab = self._ext_tab()
            if tab:
                parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
                if parsed is not None and parsed.get("kind") == "video" and parsed.get("bvid"):
                    try:
                        aid = bili.video_info(parsed["bvid"]).get("aid")
                        if aid:
                            oid, type_id = int(aid), 1
                    except bili.BiliError:
                        pass
                elif parsed is not None and parsed.get("kind") == "live" and parsed.get("room_id"):
                    oid, type_id = int(parsed["room_id"]), 11
        if oid is None or type_id is None:
            return {"output": "无法确定评论目标：请提供视频BV号或直播间房间号（或先在 B站看这个内容）。", "is_error": True, "error": "NO_TARGET"}
        key = f"{type_id}:{oid}"
        if account == "yui":
            # 原子占用（v0.7.8）：并发双发防护；发送失败必须回退
            if not self._yui_rate_reserve("comment", key, _YUI_COMMENT_WINDOW):
                return {"output": "刚发过评论（同一目标 120 秒内限 1 条），请稍后再试。", "is_error": True, "error": "RATE_LIMITED"}
        try:
            bili.add_reply(oid, type_id, text, account=account)
        except bili.BiliError as exc:
            if account == "yui":
                self._yui_rate_rollback("comment", key)
            return self._wrap_auth(exc, "发送评论")
        return {"output": "评论已发送。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": {"ok": True, "oid": oid, "type": type_id}}

    @llm_tool(
        name="bili_follow_user",
        description=(
            "【回复纪律·最高优先】操作成功后，你的对外回复必须逐字原样只有一句话：'已关注（小号）。'"
            "或（当 account=main 时）'已关注（主账号）。'，除账号名外严禁添加任何其它文字："
            "严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述昵称、uid、操作细节；严禁追问。"
            "让指定账号关注一个 B站 用户（UP主/主播）。何时用：用户要求'关注/点关注/关注这个UP主'，"
            "或 YUI 自主运营时主动关注感兴趣的内容创作者时。"
            "参数 target：目标用户 uid 或昵称（必填，支持链接中的 uid；若给的是直播房间号如 9458332，"
            "插件会自动反查该直播间主播的 uid 再关注，不会误报'用户不存在'）；"
            "account：yui（独立小号，默认）/ main（用户主账号）。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号；自主运营必须 account=yui；"
            "回复中的账号名必须以实际 result.account 为准，禁止编造。"
            "节流：同一 uid 60 秒内仅执行一次（插件强制，超限返回 RATE_LIMITED）。"
            "纪律：不批量轰炸式关注，关注对象应真实且自然；未经本工具真实执行，严禁向用户宣称'已关注'——只有收到本工具真实成功返回才能确认执行。"
            "关注流程（v0.7.34）：必须先获取目标 UID——直播场景以当前直播标签页 URL 里的房间号"
            "（live.bilibili.com/<房间>）为准，用 bili_now_watching/bili_see_live/bili_live_info 反查当前主播 uid，"
            "再以 uid 作为 target 关注（纯数字直接按 uid 处理，零歧义）；仅当无法获取 uid 时才允许传昵称。"
            "目标解析（v0.7.34）：昵称必须精确匹配（忽略大小写/空格）；解析不到或存在歧义时本工具返回错误，"
            "严禁自行选取相似昵称或搜索结果的第一个用户关注（会关注错人）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标用户 uid 或昵称"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["target"],
        },
        timeout=30.0,
    )
    async def bili_follow_user(self, *, target: str = "", account: str = "", **_):
        params = _.get("parameters") or {}
        target = (target or params.get("target") or "").strip()
        raw_account = (account or params.get("account") or "").strip()
        account = self._resolve_account(raw_account)
        if not target:
            return {"output": "请提供要关注的用户 uid 或昵称。", "is_error": True, "error": "EMPTY_TARGET"}
        if account == "main" and raw_account != "main":
            # 用户未显式指定主账号，是"默认小号"回退来的：小号未绑定，禁止静默操作主账号
            return {"output": "YUI 小号尚未绑定（当前只有主账号），请先在浏览器登录小号完成绑定；如需操作主账号请显式指定 account=main。", "is_error": True, "error": "NO_YUI_ACCOUNT"}
        key = f"follow:{target}"
        if account == "yui":
            if not self._yui_rate_reserve("relation", key, 60):
                return {"output": "刚执行过关注（同一目标 60 秒内限 1 次），请稍后再试。", "is_error": True, "error": "RATE_LIMITED"}
        try:
            result = bili.modify_relation(target, "follow", account=account)
        except bili.BiliError as exc:
            if account == "yui":
                self._yui_rate_rollback("relation", key)
            return self._wrap_auth(exc, "关注")
        label = "主账号" if account == "main" else "小号"
        return {"output": f"已关注（{label}）。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": result}

    @llm_tool(
        name="bili_unfollow_user",
        description=(
            "【回复纪律·最高优先】操作成功后，你的对外回复必须逐字原样只有一句话：'已取消关注（小号）。'"
            "或（当 account=main 时）'已取消关注（主账号）。'，除账号名外严禁添加任何其它文字："
            "严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述昵称、uid、操作细节；严禁追问。"
            "让指定账号取消关注一个 B站 用户（UP主/主播）。何时用：用户要求'取关/取消关注/拉黑前先取关'，"
            "或 YUI 自主运营时清理不再关注的创作者时。"
            "参数 target：目标用户 uid 或昵称（必填；若给的是直播房间号如 9458332，"
            "插件会自动反查该直播间主播的 uid 再取关）；"
            "account：yui（独立小号，默认）/ main（用户主账号）。"
            "账号规则：默认 YUI 独立小号；小号未配置自动回退主账号；自主运营必须 account=yui；"
            "回复中的账号名必须以实际 result.account 为准，禁止编造。"
            "节流：同一 uid 60 秒内仅执行一次（插件强制，超限返回 RATE_LIMITED）。"
            "纪律：仅在确实不再需要关注时取关，动作自然；未经本工具真实执行，严禁向用户宣称'已取消关注'——只有收到本工具真实成功返回才能确认执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标用户 uid 或昵称"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["target"],
        },
        timeout=30.0,
    )
    async def bili_unfollow_user(self, *, target: str = "", account: str = "", **_):
        params = _.get("parameters") or {}
        target = (target or params.get("target") or "").strip()
        raw_account = (account or params.get("account") or "").strip()
        account = self._resolve_account(raw_account)
        if not target:
            return {"output": "请提供要取消关注的用户 uid 或昵称。", "is_error": True, "error": "EMPTY_TARGET"}
        if account == "main" and raw_account != "main":
            return {"output": "YUI 小号尚未绑定（当前只有主账号），请先在浏览器登录小号完成绑定；如需操作主账号请显式指定 account=main。", "is_error": True, "error": "NO_YUI_ACCOUNT"}
        key = f"unfollow:{target}"
        if account == "yui":
            if not self._yui_rate_reserve("relation", key, 60):
                return {"output": "刚执行过取关（同一目标 60 秒内限 1 次），请稍后再试。", "is_error": True, "error": "RATE_LIMITED"}
        try:
            result = bili.modify_relation(target, "unfollow", account=account)
        except bili.BiliError as exc:
            if account == "yui":
                self._yui_rate_rollback("relation", key)
            return self._wrap_auth(exc, "取消关注")
        label = "主账号" if account == "main" else "小号"
        return {"output": f"已取消关注（{label}）。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": result}

    @llm_tool(
        name="bili_yui_dynamic",
        description=(
            "【发送纪律·最高优先】发送成功后，你的对外回复必须逐字原样只有一句话：'动态已发布。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀；严禁复述动态内容。"
            "用 YUI 独立小号发一条 B站文字动态（账号日常运营）。何时用：YUI 自主运营任务中想表达观点/"
            "分享内容时，或用户要求'发条动态'时。参数 text：动态内容（最长 2000 字）。"
            "节流：每天最多 6 条（插件强制，超限返回 RATE_LIMITED）。"
            "纪律：内容自然、不刷屏、不攻击。"
        ),
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "动态内容，最长 2000 字"}},
            "required": ["text"],
        },
        timeout=30.0,
    )
    async def bili_yui_dynamic(self, *, text: str = "", **_):
        params = _.get("parameters") or {}
        text = (text or params.get("text") or "").strip()
        if not text:
            return {"output": "请提供动态内容。", "is_error": True, "error": "EMPTY_MSG"}
        if not self._yui_daily_reserve("dynamic"):
            return {"output": "今天动态已发满 6 条，明天再继续。", "is_error": True, "error": "RATE_LIMITED"}
        try:
            bili.post_dynamic(text, account="yui")
        except bili.BiliError as exc:
            self._yui_daily_release("dynamic")
            return self._wrap_auth(exc, "发布动态")
        return {"output": "动态已发布。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": {"ok": True}}

    @llm_tool(
        name="bili_yui_upload",
        description=(
            "【发送纪律·最高优先】投稿提交后，你的对外回复必须逐字原样只有一句话：'稿件已提交审核。'"
            "严禁添加任何其它文字：严禁'喵/搞定/任务搞定啦/完成啦/做好啦/发出去了/发出去啦/任务完成/搞定啦'等"
            "任何口语化完成话术、表情、开场白与后缀。"
            "用 YUI 独立小号投稿一个视频（web 分片上传 + 提交审核）。何时用：YUI 自主运营任务中"
            "发现插件目录 yui_uploads 有新的视频文件时，或用户要求'投稿/发视频'时。"
            "参数 file：yui_uploads 目录下的文件名（仅接受该目录内文件，禁止路径穿越）；"
            "title：稿件标题（必填）；desc：简介；tags：标签（逗号分隔）；tid：分区 id（默认 171 电子竞技）。"
            "节流：每天最多 2 个稿件（插件强制）。投稿走审核流程，需小号有投稿权限。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "yui_uploads 目录下的视频文件名"},
                "title": {"type": "string", "description": "稿件标题，必填"},
                "desc": {"type": "string", "description": "稿件简介（可选）"},
                "tags": {"type": "string", "description": "标签，逗号分隔（可选）"},
                "tid": {"type": "integer", "description": "分区 id，默认 171（电子竞技）"},
            },
            "required": ["file", "title"],
        },
        timeout=300.0,
    )
    async def bili_yui_upload(self, *, file: str = "", title: str = "", desc: str = "", tags: str = "", tid: int = 0, **_):
        params = _.get("parameters") or {}
        file = (file or params.get("file") or "").strip()
        title = (title or params.get("title") or "").strip()
        desc = (desc or params.get("desc") or "").strip()
        tags = (tags or params.get("tags") or "").strip()
        try:
            tid = int(tid if tid else params.get("tid") or 171)
        except (TypeError, ValueError):
            tid = 171
        if not file:
            return {"output": "请提供 yui_uploads 目录下的视频文件名。", "is_error": True, "error": "NO_FILE"}
        if not title:
            return {"output": "请提供稿件标题。", "is_error": True, "error": "NO_TITLE"}
        if not self._yui_daily_reserve("upload"):
            return {"output": "今天已投稿 2 个，明天再继续。", "is_error": True, "error": "RATE_LIMITED"}
        # 安全：仅允许插件目录 yui_uploads 内的文件（拒绝路径穿越/任意文件上传）
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), _YUI_UPLOAD_DIR)
        base = os.path.basename(file)
        if base in ("", ".", "..") or "/" in file or "\\" in file or ":" in file:
            self._yui_daily_release("upload")
            return {"output": "只接受 yui_uploads 目录内的文件名（禁止路径分隔符）。", "is_error": True, "error": "BAD_PATH"}
        file_path = os.path.join(upload_dir, base)
        if not os.path.isfile(file_path):
            self._yui_daily_release("upload")
            return {"output": f"yui_uploads 目录下没有这个文件：{base}（请先把视频文件放到插件目录的 yui_uploads 文件夹）", "is_error": True, "error": "NO_FILE"}
        # 投稿互斥（复查优化 v0.7.8）：同一时刻只允许一个上传流程（分钟级长任务）。
        # 大号/小号同时行动时，自主引擎与手动调用可能并发触发投稿：
        # 互斥锁保证后到者返回 BUSY，且投稿串行执行使天级计数天然不被并发突破。
        with self._yui_upload_lock:
            if self._yui_uploading:
                self._yui_daily_release("upload")
                return {"output": "已有投稿正在进行中，请稍后再试。", "is_error": True, "error": "BUSY"}
            self._yui_uploading = True
        try:
            bili.submit_video(file_path, title, desc=desc, tags=tags, tid=tid, account="yui")
        except bili.BiliError as exc:
            self._yui_daily_release("upload")
            return self._wrap_auth(exc, "投稿")
        finally:
            with self._yui_upload_lock:
                self._yui_uploading = False
        return {"output": "稿件已提交审核。", "is_error": False, "meta": {"agent": {"delivery": "silent"}}, "result": {"ok": True, "file": base, "title": title}}

    @llm_tool(
        name="bili_yui_status",
        description=(
            "查询 YUI 独立小号（B站）的绑定状态与账号信息。何时用：用户问'小号绑定好了吗''YUI 的账号是什么'"
            "，或 YUI 自主运营任务启动前确认小号可用性时。"
            "返回：小号昵称/uid/等级/是否配置。未配置时给出配置方法（BILI_YUI_COOKIE 环境变量或插件目录 yui_cookie.txt）。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=20.0,
    )
    async def bili_yui_status(self, **_):
        if not bili._cookie("yui"):
            return {
                "output": (
                    "YUI 小号未配置。配置方法：把 B站小号的完整 Cookie（含 SESSDATA 与 bili_jct）"
                    "保存到插件目录的 yui_cookie.txt 文件（或设置环境变量 BILI_YUI_COOKIE）。"
                    "也可以打开接入面板绑定第二个账号：第一个登录的账号自动成为大号，第二个登录的自动成为小号（v0.7.10）。"
                    "配置后 YUI 即可用独立小号跟随你同步行动、自主发弹幕/评论/动态/投稿，与你的主账号完全隔离。"
                ),
                "is_error": True,
                "error": "NO_YUI_COOKIE",
            }
        try:
            info = bili.my_info("yui")
        except bili.BiliError as exc:
            return _wrap_err(exc)
        return {
            "output": f"YUI 小号：{info['uname']}（uid {info['mid']}，Lv{info['level']}）｜ 配置正常，可自主运营",
            "is_error": False,
            "result": info,
        }

    @llm_tool(
        name="bili_accounts",
        description=(
            "查询当前绑定的 B站大小号视图：大号（main，第一个登录的账号）与小号（yui，第二个登录的账号，"
            "默认操作账号）各自的昵称/uid/来源/绑定时间。何时用：用户问'哪个是大号/小号''现在用的哪个账号'"
            "'YUI 用的是谁的账号'，或 YUI 需要分清大小号再行动时。"
            "返回两个账号的 uid（可从 uid 判断身份），未配置的账号标注'未配置'。"
            "大小号归属默认为绑定顺序：第一个登录=大号，第二个登录=小号；如需交换请用 bili_set_roles。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=25.0,
    )
    async def bili_accounts(self, **_):
        accounts = _read_accounts()
        rows = []
        for role, label in (("main", "大号(main，第一个登录的账号)"), ("yui", "小号(yui，第二个登录的账号，默认操作)")):
            cookie = bili._cookie(role)
            uid = accounts.get(role, {}).get("uid")
            bound_at = accounts.get(role, {}).get("bound_at")
            if not cookie:
                rows.append(f"{label}：未配置")
                continue
            if uid is None:
                uid = bili.cookie_uid(cookie)
            if bound_at:
                try:
                    bound_txt = time.strftime("%m-%d %H:%M", time.localtime(float(bound_at)))
                except (TypeError, ValueError, OSError):
                    bound_txt = ""
            else:
                bound_txt = ""
            try:
                info = bili.my_info(role)
                ident = f"{info['uname']}（uid {info['mid']}，Lv{info['level']}）"
            except bili.BiliError:
                ident = f"uid {uid if uid is not None else '未知'}"
            rows.append(f"{label}：{ident}｜来源{('绑定于 ' + bound_txt) if bound_txt else '配置'}")
        default_txt = "yui（小号）" if bili._cookie("yui") else "main（大号，小号未配置时的回退）"
        return {
            "output": "\n".join(rows) + f"\n未说明时默认使用：{default_txt}",
            "is_error": False,
            "result": {
                "main": {
                    "uid": accounts.get("main", {}).get("uid"),
                    "bound_at": accounts.get("main", {}).get("bound_at"),
                    "configured": bool(bili._cookie("main")),
                },
                "yui": {
                    "uid": accounts.get("yui", {}).get("uid"),
                    "bound_at": accounts.get("yui", {}).get("bound_at"),
                    "configured": bool(bili._cookie("yui")),
                },
                "default_role": "yui" if bili._cookie("yui") else "main",
            },
        }

    @llm_tool(
        name="bili_set_roles",
        description=(
            "修改大小号归属（v0.7.10）：把两个已绑定账号的角色互换，或按 uid 把指定账号设为指定角色。"
            "何时用：用户后期说明'把小号当大号用''交换一下大小号''以后都用那个账号操作'时。"
            "参数 action：swap=交换大小号（默认）；uid：要设为目标角色的账号 uid；role：目标角色（main 或 yui）。"
            "规则：大小号归属默认按绑定顺序（第一个登录=大号，第二个登录=小号），本工具用于后期修改；"
            "仅支持文件层配置的账号互换（环境变量优先时请手动调整 BILI_COOKIE / BILI_YUI_COOKIE 后重试）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["swap"], "description": "swap=交换大小号（默认）"},
                "uid": {"type": "integer", "description": "目标账号 uid（与 role 配合：把该账号设为指定角色）"},
                "role": {"type": "string", "enum": ["main", "yui"], "description": "目标角色（与 uid 配合）"},
            },
            "required": [],
        },
        timeout=20.0,
    )
    async def bili_set_roles(self, *, action: str = "", uid: int = 0, role: str = "", **_):
        params = _.get("parameters") or {}
        action = (action or params.get("action") or "swap").strip()
        uid = int(uid if uid else params.get("uid") or 0) or 0
        role = (role or params.get("role") or "").strip()
        main_file = bili._main_cookie_from_file()
        yui_file = bili._yui_cookie_from_file()
        # 安全（v0.7.10）：只有账号 cookie 与文件层完全一致（绑定流程持久化）才允许互换；
        # 来自环境变量/配置覆盖的账号无法程序化互换，提示手动调整，避免误清空账号。
        main_effective = bili._cookie("main")
        yui_effective = bili._cookie("yui")
        if main_effective and main_effective != main_file:
            return {"output": "大号 Cookie 来自环境变量或配置（非文件层），无法自动互换；请手动调整 BILI_COOKIE 后重试。", "is_error": True, "error": "ENV_LOCKED"}
        if yui_effective and yui_effective != yui_file:
            return {"output": "小号 Cookie 来自环境变量或配置（非文件层），无法自动互换；请手动调整 BILI_YUI_COOKIE 后重试。", "is_error": True, "error": "ENV_LOCKED"}
        if not main_file and not yui_file:
            return {"output": "当前没有文件层绑定的账号，无法修改角色；请先通过接入面板绑定两个账号（第一个登录=大号，第二个登录=小号）。", "is_error": True, "error": "NO_ACCOUNTS"}
        # 定位目标账号当前角色（uid 指定时）
        if uid and role in ("main", "yui"):
            cur = None
            for r in ("main", "yui"):
                c = bili._cookie(r)
                if c and bili.cookie_uid(c) == uid:
                    cur = r
                    break
            if cur is None:
                return {"output": f"没有绑定 uid={uid} 的账号。", "is_error": True, "error": "NO_ACCOUNT"}
            if cur == role:
                return {"output": f"uid={uid} 的账号本来就是{role}，无需修改。", "is_error": False, "result": {"ok": True, "unchanged": True}}
            # uid 账号当前角色≠目标角色 → 等价于交换大小号
        # 统一动作：交换大小号（文件互换 + 角色映射互换 + 运行期 env 同步）
        bili.write_yui_cookie_file(main_file)
        bili.write_main_cookie_file(yui_file)
        accounts = _read_accounts()
        main_rec, yui_rec = accounts.get("main") or {}, accounts.get("yui") or {}
        accounts["main"], accounts["yui"] = yui_rec, main_rec
        _write_accounts(accounts)
        os.environ["BILI_COOKIE"] = bili._main_cookie_from_file()
        os.environ["BILI_YUI_COOKIE"] = bili._yui_cookie_from_file()
        if uid and role in ("main", "yui"):
            return {"output": f"已把 uid={uid} 的账号设为{role}（原{role}的账号自动换到另一角色）。", "is_error": False, "result": {"ok": True, "uid": uid, "role": role}}
        return {"output": "已交换大小号：原小号现在是大号，原大号现在是小号（未说明时仍默认操作小号）。", "is_error": False, "result": {"ok": True, "swapped": True}}

    @llm_tool(
        name="bili_see_screen",
        description=(
            "读取用户当前标签页的画面截图（浏览器扩展 captureVisibleTab 截取，"
            "JPEG 已保存到本地 _SCREENSHOT_PATH）。何时用：用户问'你看得到画面吗''你看得到屏幕吗'"
            "'直播画面你能看到吗'，或 YUI 需要描述画面本身（非文字实况）时。"
            "无需参数；截图 120 秒内新鲜，超出后提示稍后重试。"
            "回复纪律：看到画面后直接描述画面内容（直播画面/游戏画面等），不加'系统又同步/我看到/你正在看'等开场白；"
            "看到/没看到都是感知实况，不是'任务'——严禁'任务完成/搞定啦/完成啦'等完成话术与汇报腔；"
            "看不到时如实说明，禁止说'系统刚给我确认了'等话术，禁止猜测。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=20.0,
    )
    async def bili_see_screen(self, **_):
        shot = self._ext_shot()
        if not shot:
            return {
                "output": "暂时没有新鲜画面截图（扩展未截取或已过期）。",
                "is_error": True,
                "error": "NO_SHOT",
            }
        path = shot.get("path") or _SCREENSHOT_PATH
        return {
            "output": f"已读取你屏幕当前画面截图（{shot.get('title') or shot.get('url') or ''}，"
            f"保存于 {path}，{shot.get('size') or 0} 字节）。",
            "is_error": False,
            "meta": {"agent": {"delivery": "silent"}},
            "result": {"path": path, "title": shot.get("title", ""), "at": shot.get("at", 0)},
        }

    @llm_tool(
        name="bili_listen_audio",
        description=(
            "读取用户当前 B站 标签页的声音转写（浏览器扩展后台采集标签页音频，"
            "本机 faster-whisper 转成文字，全部本地处理、不上外网）。何时用：用户问"
            "'你听到声音吗''直播里在说什么''视频在讲什么'，或 YUI 需要知道直播/视频"
            "里正在说的话（如主播念弹幕、解说内容）时。无需参数；转写结果 30 秒内新鲜，"
            "过期或无声时如实说明。"
            "回复纪律：听到内容后直接转述内容本身（主播/视频里正在说的话），"
            "不加'我听到了/系统转写了/刚刚的声音是'等开场白；"
            "严禁'喵/喵呜/呜'等语气词、表情与客套话开头（回复直接以内容本身开始）；"
            "没听到时只如实说明（如'目前没听到声音'），严禁猜测原因——"
            "严禁'是不是主播暂时没说话/是不是静音了/是不是麦克风坏了/是不是开了弹幕过滤'等任何猜测句式；"
            "严禁复述或模仿别人刚说过的话（如群友刚问过的'没听到声音'，不要再原样重复一遍）；"
            "只在用户询问或需要感知内容时调用，没人问就不主动汇报。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=20.0,
    )
    async def bili_listen_audio(self, **_):
        asr = self._ext_asr_text()
        if not asr:
            # 如实说明没听到；诊断细节放 result（YUI 可据此如实转述，禁止猜测原因）
            state = getattr(self, "_ext_audio_state", None) or {}
            last_audio = getattr(self, "_ext_audio_data", None)
            reason = "no_audio_received"
            if not (state.get("enabled")):
                reason = "capture_disabled"
            elif not last_audio:
                reason = "no_audio_received"
            else:
                reason = "silence_or_stale"
            return {
                "output": "暂时没有听到声音。",
                "is_error": True,
                "error": "NO_AUDIO",
                "meta": {"agent": {"delivery": "silent"}},
                "result": {
                    "reason": reason,
                    "audio_enabled": bool(state.get("enabled")),
                    "audio_enabled_at": state.get("at"),
                    "last_audio_at": last_audio.get("at") if last_audio else None,
                },
            }
        return {
            "output": asr["text"],
            "is_error": False,
            "meta": {"agent": {"delivery": "silent"}},
            "result": {"text": asr["text"], "at": asr.get("at", 0)},
        }

    # ══════════ 自动路由入口（plugin_entry：TaskExecutor 评估可见） ══════════
    # llm_tool 走 LLM 函数调用通道；plugin_entry 走 TaskExecutor 的 UserPlugin
    # 路由通道（评估时 LLM 依据插件 entries 判断"哪个插件能执行该请求"并路由）。
    # 此前 bili_viewer 只有 llm_tool、无任何 plugin_entry，导致评估判定
    # "可用插件列表中无支持发送弹幕的插件" → YUI 永远无法路由发弹幕。这里注册
    # run（通用兜底）与 bili_danmaku（弹幕专用）两个入口修复该问题。
    #
    # 【重要】方法名禁止以下划线开头：宿主 collect_entries() 会跳过所有
    # `_` 前缀的实例方法（plugin/sdk/shared/core/base.py），导致执行侧
    # "Entry 'xxx' not found"（v0.6.22 教训）。entry_run / entry_danmaku 的
    # id 由 plugin_entry(id=...) 指定，方法名仅需避开下划线前缀即可。

    @plugin_entry(
        id="run",
        name="B站全能助手",
        description=(
            "B站全能助手（bili_viewer）：感知用户当前正在看的B站直播/视频"
            "（直播间房间号/视频BV号，编号仅供内部操作，回复用户时请勿复述）；"
            "向B站直播间或视频发送弹幕、给视频/直播写评论（bili_reply_comment）；"
            "YUI 独立小号运营（bili_yui_dynamic 发动态 / bili_yui_upload 投稿 / bili_yui_status 查小号状态）；"
            "查看视频详情/弹幕/字幕/AI概要/评论、直播间信息与实时弹幕、UP主信息与投稿；"
            "搜索视频/直播/用户；查看和修改自己的稿件；查询B站账号绑定状态。"
            "用户请求'发弹幕''写评论''发动态''投稿''看视频详情''搜B站''看直播''查UP主'等B站相关操作时使用本插件。"
            "账号规则：写操作（弹幕/评论/动态/投稿）默认使用 YUI 独立小号（account=yui，未配置时回退主账号）；"
            "用户明确要求用自己的账号时传 account=main。自主运营任务中必须 account=yui，严禁用主账号。"
            "回复纪律：发送成功后只原样回复'弹幕已发送。'/'评论已发送。'，禁止'搞定/任务搞定啦/发出去咯'话术、"
            "禁止复述房间号/BV号/弹幕内容、禁止追问'你看到了吗'、禁止猜测弹幕为何没显示；"
            "感知/查询只陈述实况本身，禁止'系统又同步/系统刚给我确认了/房间号没错'等开场白与确认话术；"
            "感知/查询不是'任务'——严禁'任务完成/搞定啦/完成啦/已确认'等任何完成话术与汇报腔；"
            "严禁主动询问'要不要发弹幕/要不要回复弹幕'——弹幕仅在用户明确要求或自主运营任务中发送。"
            "发送纪律：弹幕发送入口调用一次即完成，禁止发送后再次调用本入口或其他入口确认/补发/重复发送"
            "（同目标同内容 60 秒内自动去重，只会发一次）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作类型：send_live_danmaku（发直播弹幕，需 room+text）/ send_video_danmaku（发视频弹幕，需 bvid+text）/ reply（写评论，需 text 与 bvid 或 room）/ dynamic（发动态，需 text）/ upload（投稿，需 file 为 yui_uploads 内文件名 + title）/ query（查询与感知）",
                },
                "room": {"type": "string", "description": "直播间房间号或链接（发直播弹幕时必填）"},
                "bvid": {"type": "string", "description": "视频BV号（发视频弹幕时必填）"},
                "text": {"type": "string", "description": "弹幕/评论/动态内容（直播最长 30 字、视频弹幕最长 100 字、评论最长 1000 字、动态最长 2000 字）；action=upload 时填 yui_uploads 目录内的视频文件名"},
                "title": {"type": "string", "description": "仅投稿（upload）时使用：稿件标题，必填"},
                "desc": {"type": "string", "description": "仅投稿（upload）时使用：稿件简介（可选）"},
                "tags": {"type": "string", "description": "仅投稿（upload）时使用：标签，逗号分隔（可选）"},
                "progress": {"type": "integer", "description": "视频弹幕出现的时间点（秒），默认 0 即开头"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["action"],
        },
        timeout=60.0,
    )
    async def entry_run(self, **kwargs):
        params = kwargs.get("parameters") or {}
        action = kwargs.get("action") or params.get("action") or ""
        room = str(kwargs.get("room") or params.get("room") or "")
        bvid = str(kwargs.get("bvid") or params.get("bvid") or "")
        text = str(kwargs.get("text") or params.get("text") or "")
        account = str(kwargs.get("account") or params.get("account") or "")
        try:
            progress = int(kwargs.get("progress") or params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if action == "reply":
            return await self.bili_reply_comment(target=(bvid or room), text=text, account=account)
        if action == "dynamic":
            return await self.bili_yui_dynamic(text=text)
        if action == "upload":
            return await self.bili_yui_upload(
                file=text,
                title=str(params.get("title") or kwargs.get("title") or ""),
                desc=str(params.get("desc") or kwargs.get("desc") or ""),
                tags=str(params.get("tags") or kwargs.get("tags") or ""),
            )
        if action in ("send_live_danmaku", "live_danmaku"):
            return await self.bili_live_send_danmaku(room=room, text=text, account=account)
        if action in ("send_video_danmaku", "video_danmaku"):
            return await self.bili_video_send_danmaku(bvid=bvid, text=text, progress=progress, account=account)
        # 兜底①：LLM 未给 action 但显式给了目标 → 按目标直接发弹幕
        if text and bvid:
            return await self.bili_video_send_danmaku(bvid=bvid, text=text, progress=progress, account=account)
        if text and room:
            return await self.bili_live_send_danmaku(room=room, text=text, account=account)
        # 兜底②：感知当前正在看的视频/直播 → 有弹幕内容时自动发送
        # （parse_bili_url 键名为 kind/room_id/bvid，无 'type' 键，v0.6.23 KeyError 教训）
        tab = self._ext_tab()
        if tab:
            parsed = bili.parse_bili_url(tab.get("url", ""), tab.get("title", ""))
            if parsed is not None:
                kind = parsed.get("kind")
                if text and kind == "video" and parsed.get("bvid"):
                    return await self.bili_video_send_danmaku(bvid=parsed["bvid"], text=text, progress=progress, account=account)
                if text and kind == "live" and parsed.get("room_id"):
                    return await self.bili_live_send_danmaku(room=parsed["room_id"], text=text, account=account)
                return {
                    "output": (
                        f"（后台静默感知到的实况，不是任务、无需汇报：用户当前正在看「{tab.get('title', '')}」（{kind or '未知'}）。"
                        "直接以实况回答用户即可；严禁'任务完成/搞定啦/完成啦/已确认'等完成话术与开场白。）"
                    ),
                    "is_error": False,
                    "meta": {"agent": {"delivery": "silent"}},
                    "result": parsed,
                }
        return {
            "output": "请指定操作：发弹幕（action=send_live_danmaku/send_video_danmaku，需 room/bvid 与 text）、写评论（reply）、发动态（dynamic）、投稿（upload），或查询B站内容。",
            "is_error": True,
            "error": "NO_ACTION",
        }

    @plugin_entry(
        id="bili_danmaku",
        name="发送B站弹幕",
        description=(
            "向B站直播间或视频发送弹幕。用户明确说'发弹幕''发一条弹幕''帮我发弹幕'时使用；"
            "严禁主动询问'要不要发弹幕/要不要回复弹幕'——用户没要求就绝不调用。"
            "直播弹幕需房间号（room），视频弹幕需BV号（bvid），内容为 text；"
            "account 可选：yui=YUI 独立小号（默认）/ main=用户主账号。"
            "发送后只原样回复'弹幕已发送。'，禁止'搞定/任务搞定啦/发出去咯'话术、禁止复述房间号/BV号/弹幕内容、"
            "禁止追问'你看到了吗'、禁止猜测弹幕为何没显示。需要已配置 B站账号（小号或主账号）。"
            "本入口调用一次即完成发送，禁止发送后再次调用本入口或 run 等其他入口确认/补发/重复发送同一弹幕"
            "（同目标同内容 60 秒内自动去重，只会发一次）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "room": {"type": "string", "description": "直播间房间号或链接（发直播弹幕时必填）"},
                "bvid": {"type": "string", "description": "视频BV号（发视频弹幕时必填）"},
                "text": {"type": "string", "description": "弹幕内容，直播最长 30 字、视频最长 100 字"},
                "progress": {"type": "integer", "description": "视频弹幕出现的时间点（秒），默认 0 即开头"},
                "account": {"type": "string", "description": "yui=YUI 独立小号（默认）/ main=用户主账号"},
            },
            "required": ["text"],
        },
        timeout=30.0,
    )
    async def entry_danmaku(self, **kwargs):
        params = kwargs.get("parameters") or {}
        room = str(kwargs.get("room") or params.get("room") or "")
        bvid = str(kwargs.get("bvid") or params.get("bvid") or "")
        text = str(kwargs.get("text") or params.get("text") or "")
        account = str(kwargs.get("account") or params.get("account") or "")
        try:
            progress = int(kwargs.get("progress") or params.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, progress)
        if bvid:
            return await self.bili_video_send_danmaku(bvid=bvid, text=text, progress=progress, account=account)
        if room:
            return await self.bili_live_send_danmaku(room=room, text=text, account=account)
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

    def _assign_cookie(self, cookie: str, at: int, source: str) -> dict:
        """按登录顺序把 Cookie 归位到账号（v0.7.10）：第一个登录=大号(main)，第二个登录=小号(yui)。

        - 与当前大号同一 uid → 刷新大号（更新时间戳）；
        - 与当前小号同一 uid → 刷新小号；
        - 尚未有大号 → 归为大号并持久化（main_cookie.txt）；
        - 已有大号且 uid 不同 → 归为小号并持久化（yui_cookie.txt，默认操作账号）；
        - 大小号都已存在且新 uid 都不匹配 → 拒绝（提示先说明替换哪个角色）。
        结果同步写入环境变量（运行期生效）+ accounts.json（角色映射记忆）。
        """
        ts = float(at) if at and at > 0 else time.time()
        self._ext_last_seen_ts = ts
        cookie = str(cookie or "").strip()
        if "SESSDATA=" not in cookie:
            return {"ok": True, "bound": False, "detail": "扩展已推送，但浏览器中没有有效 B站登录态"}
        uid = bili.cookie_uid(cookie)
        accounts = _read_accounts()
        main_uid = accounts.get("main", {}).get("uid")
        yui_uid = accounts.get("yui", {}).get("uid")
        now = time.time()
        if uid is not None and uid == main_uid:
            # 刷新大号
            accounts.setdefault("main", {})["bound_at"] = now
            _write_accounts(accounts)
            os.environ["BILI_COOKIE"] = cookie
            self._cookie_source = source
            return {"ok": True, "bound": True, "detail": "已刷新大号（main）登录态"}
        if uid is not None and uid == yui_uid:
            # 刷新小号
            accounts.setdefault("yui", {})["bound_at"] = now
            _write_accounts(accounts)
            os.environ["BILI_YUI_COOKIE"] = cookie
            return {"ok": True, "bound": True, "detail": "已刷新小号（yui）登录态"}
        if main_uid is None:
            # 第一个登录的账号 → 大号（持久化到 main_cookie.txt）
            bili.write_main_cookie_file(cookie)
            accounts["main"] = {"uid": uid, "bound_at": now}
            _write_accounts(accounts)
            os.environ["BILI_COOKIE"] = cookie
            self._cookie_source = source
            return {"ok": True, "bound": True, "detail": "已绑定大号（第一个登录的账号，main）"}
        # 已有大号且新 uid 不同 → 第二个登录的账号自动成为小号（yui，默认操作账号）
        if yui_uid is None or uid is None:
            bili.write_yui_cookie_file(cookie)
            accounts["yui"] = {"uid": uid, "bound_at": now}
            _write_accounts(accounts)
            os.environ["BILI_YUI_COOKIE"] = cookie
            return {"ok": True, "bound": True, "detail": "已绑定小号（第二个登录的账号，yui），未说明时默认用它操作"}
        return {"ok": True, "bound": False, "detail": "大小号都已绑定；新账号未匹配，如需替换请说明要替换大号还是小号"}

    def _receive_ext_cookie(self, cookie: str, at: int, source: str | None = None) -> dict:
        """接收浏览器扩展（B站接入桥）推送的登录态：按登录顺序归位大小号（v0.7.10）。

        仅接受 127.0.0.1 本机来源（面板服务本身只监听本机）；cookie 不上传不写日志。
        扩展推送的 at 为空（首次握手）时用当前时间。
        v0.7.21：记录来源浏览器（EXT_SOURCE），供 _ext_tab 多扩展并存时过滤标签页。
        """
        self._ext_source = str(source or "").strip()
        return self._assign_cookie(cookie, at, _COOKIE_SOURCE_EXTENSION)

    def _receive_ext_tab(self, url: str, title: str, at: int, source: str | None = None) -> dict:
        """接收浏览器扩展推送的当前活动标签页（URL + 标题），供精确感知。

        tab 数据仅本地使用：驱动 bili_now_watching 与同步 watcher 的精确感知，
        不上传、不写日志。v0.7.21：记录来源浏览器（EXT_SOURCE），供 _ext_tab
        在 Chrome/Edge/360 多扩展并存时按 cookie 来源过滤，避免互相覆盖。
        """
        ts = float(at) if at and at > 0 else time.time()
        self._ext_last_seen_ts = ts
        self._ext_tab_data = {
            "url": str(url or "").strip(),
            "title": str(title or "").strip(),
            "source": str(source or "").strip(),
            "at": ts,
        }
        return {"ok": True, "detail": "已记录当前活动标签页"}

    def _receive_ext_dom(self, body: dict) -> dict:
        """接收浏览器扩展注入脚本读到的页面 DOM 实况（直播弹幕/视频进度等）。

        DOM 数据仅本地使用：为 bili_see_live / bili_see_video 提供页面级实时内容，
        不上传、不写日志。
        """
        ts = float(body.get("at") or 0) or time.time()
        raw_dms = body.get("danmaku") or []
        if isinstance(raw_dms, list):
            # 条数与单条长度都设上限，防止异常/恶意 DOM 推送撑爆内存与后续渲染
            danmaku = [str(x)[: self._EXT_DOM_MAX_DM_LEN] for x in raw_dms[: self._EXT_DOM_MAX_DANMAKU]]
        else:
            danmaku = []
        self._ext_dom_data = {
            "url": str(body.get("url") or "").strip(),
            "kind": str(body.get("kind") or "").strip(),
            "title": str(body.get("title") or "")[:200],
            "uname": str(body.get("uname") or "")[:80],
            "online": str(body.get("online") or ""),
            "area": str(body.get("area") or ""),
            "parent_area": str(body.get("parent_area") or ""),
            "progress": float(body.get("progress") or 0) or 0,
            "duration": float(body.get("duration") or 0) or 0,
            "danmaku": danmaku,
            "at": ts,
        }
        _logger.info(
            "ext dom received: kind=%s title=%s online=%s dms=%d at=%s",
            self._ext_dom_data["kind"],
            self._ext_dom_data["title"][:30],
            self._ext_dom_data["online"],
            len(self._ext_dom_data["danmaku"]),
            int(ts),
        )
        return {"ok": True, "detail": "已记录页面DOM实况"}

    def _ext_dom(self) -> dict | None:
        """返回新鲜（120s 内）的扩展页面 DOM 实况；无或过期返回 None。"""
        dom = getattr(self, "_ext_dom_data", None)
        if not dom:
            return None
        if time.time() - float(dom.get("at") or 0) > web_ui._EXT_ONLINE_WINDOW:
            return None
        return dom

    _EXT_SHOT_MAX_RAW = 20 * 1024 * 1024  # 截屏落盘上限：防恶意/异常超大 payload 撑爆磁盘
    _EXT_DOM_MAX_DANMAKU = 100  # 页面 DOM 弹幕最多保留条数
    _EXT_DOM_MAX_DM_LEN = 200  # 单条弹幕最长字符

    def _receive_ext_shot(self, body: dict) -> dict:
        """接收浏览器扩展截取的当前标签页画面（JPEG dataURL），落盘供 YUI 读取。

        画面仅本地保存（_SCREENSHOT_PATH），不上传、不写日志正文。
        仅接受 data:image/* 前缀的合法 base64，且限制落盘体积（v0.6.39）。
        """
        image = str(body.get("image") or "")
        if not image or "," not in image:
            return {"ok": False, "detail": "截屏数据为空或格式异常"}
        header = image.split(",", 1)[0].strip()
        if not header.startswith("data:image/") or not header.lower().endswith(";base64"):
            return {"ok": False, "detail": "截屏数据格式不受支持（仅 data:image/*;base64）"}
        try:
            raw = base64.b64decode(image.split(",", 1)[1], validate=True)
        except Exception as exc:
            return {"ok": False, "detail": f"截屏解码失败：{exc}"}
        if not raw:
            return {"ok": False, "detail": "截屏内容为空"}
        if len(raw) > self._EXT_SHOT_MAX_RAW:
            return {"ok": False, "detail": f"截屏体积过大（>{self._EXT_SHOT_MAX_RAW} 字节），已拒绝落盘"}
        try:
            os.makedirs(os.path.dirname(_SCREENSHOT_PATH), exist_ok=True)
            with open(_SCREENSHOT_PATH, "wb") as f:
                f.write(raw)
        except Exception as exc:
            return {"ok": False, "detail": f"截屏落盘失败：{exc}"}
        self._ext_shot_data = {
            "url": str(body.get("url") or "").strip(),
            "title": str(body.get("title") or "")[:200],
            "path": _SCREENSHOT_PATH,
            "size": len(raw),
            "at": float(body.get("at") or 0) or time.time(),
        }
        _logger.info(
            "ext shot received: url=%s title=%s bytes=%d at=%s",
            self._ext_shot_data["url"][:60],
            self._ext_shot_data["title"][:30],
            len(raw),
            int(self._ext_shot_data["at"]),
        )
        return {"ok": True, "detail": "已保存当前页面画面截图"}

    def _ext_shot(self) -> dict | None:
        """返回新鲜（120s 内）的扩展截屏元数据；无或过期返回 None。"""
        shot = getattr(self, "_ext_shot_data", None)
        if not shot:
            return None
        if time.time() - float(shot.get("at") or 0) > web_ui._EXT_ONLINE_WINDOW:
            return None
        return shot

    # ══════════ 扩展声音通道（v0.7.11：让 YUI 能"听到"直播/视频） ══════════

    def _asr_lock(self) -> threading.Lock:
        # 注意：存储属性名不能与方法名相同（self._asr_lock 首次 getattr 会取到
        # bound method 本身，导致 with 语句抛 "'method' object does not support
        # the context manager protocol"）。改用 _asr_lock_obj 存储。
        lock = getattr(self, "_asr_lock_obj", None)
        if lock is None:
            lock = threading.Lock()
            self._asr_lock_obj = lock
        return lock

    def _receive_ext_audio(self, body: dict) -> dict:
        """接收浏览器扩展推送的标签页声音段（wav base64 dataURL），落盘并排队 ASR。

        音频仅本地保存（_AUDIO_PATH）与本地转写（faster-whisper），不上传、不写日志正文。
        校验：仅接受 data:audio/wav;base64 前缀；限制落盘体积；无声段由扩展侧跳过。
        """
        audio = str(body.get("audio") or "")
        if not audio or "," not in audio:
            return {"ok": False, "detail": "声音数据为空或格式异常"}
        header = audio.split(",", 1)[0].strip()
        if not header.startswith("data:audio/") or not header.lower().endswith(";base64"):
            return {"ok": False, "detail": "声音数据格式不受支持（仅 data:audio/*;base64）"}
        try:
            raw = base64.b64decode(audio.split(",", 1)[1], validate=True)
        except Exception as exc:
            return {"ok": False, "detail": f"声音解码失败：{exc}"}
        if not raw:
            return {"ok": False, "detail": "声音内容为空"}
        if len(raw) > _AUDIO_MAX_RAW:
            return {"ok": False, "detail": f"声音体积过大（>{_AUDIO_MAX_RAW} 字节），已拒绝落盘"}
        try:
            os.makedirs(os.path.dirname(_AUDIO_PATH), exist_ok=True)
            with open(_AUDIO_PATH, "wb") as f:
                f.write(raw)
        except Exception as exc:
            return {"ok": False, "detail": f"声音落盘失败：{exc}"}
        self._ext_audio_data = {
            "path": _AUDIO_PATH,
            "sample_rate": int(body.get("sample_rate") or 0) or 16000,
            "seq": int(body.get("seq") or 0) or 0,
            "at": float(body.get("at") or 0) or time.time(),
        }
        _logger.info("ext audio received: seq=%s bytes=%d rate=%s", self._ext_audio_data["seq"], len(raw), self._ext_audio_data["sample_rate"])
        self._kick_asr()
        # v0.7.19：ASR worker 用 _asr_worker_evt 事件唤醒（wait timeout=1s），此前事件
        # 只在测试里被 set、运行时永远无人触发——worker 常眠、转写从不发生（听不到声音
        # 的真正根因，ASR 锁修复后仍不转写的漏网）。每个新声音段到达都唤醒一次，
        # worker 醒来处理最新段（串行去重，积压自动跳过）。
        evt = getattr(self, "_asr_worker_evt", None)
        if evt is not None:
            evt.set()
        return {"ok": True, "detail": "已保存当前声音段"}

    def _ext_audio(self) -> dict | None:
        """返回最近一次声音段的元数据；无则返回 None（不判新鲜：ASR 由 worker 排队处理）。"""
        return getattr(self, "_ext_audio_data", None)

    def _receive_ext_audio_state(self, body: dict) -> dict:
        """扩展推送的声音捕获开关状态（诊断用）：记录 enabled 与时间，供面板/YUI 判断链路。"""
        enabled = bool(body.get("enabled"))
        state = {"enabled": enabled, "at": float(body.get("at") or 0) or time.time()}
        if body.get("ctx_state"):
            state["ctx_state"] = str(body["ctx_state"])[:32]
        if body.get("ctx_err"):
            state["ctx_err"] = str(body["ctx_err"])[:300]
        self._ext_audio_state = state
        _logger.info("ext audio state: enabled=%s ctx_state=%s ctx_err=%s", enabled, state.get("ctx_state"), state.get("ctx_err", "")[:200])
        return {"ok": True, "detail": "已记录声音捕获状态"}

    def _find_asr_python(self) -> str | None:
        """寻找可执行外部 ASR 桥的 Python 解释器（必须装有 faster-whisper）。

        优先级：环境变量 BILI_ASR_PYTHON > 开发 venv 固定候选 > PATH 上的 python。
        返回 None 表示找不到可用解释器（声音转写降级为空，不影响插件其余功能）。
        """
        env = (os.environ.get("BILI_ASR_PYTHON") or "").strip()
        if env and os.path.isfile(env):
            return env
        candidates = [
            # N.E.K.O 源码仓库 venv（本机开发环境，已装 faster-whisper 1.2.1）
            os.path.join(os.environ.get("LOCALAPPDATA") or "", "..", "N.E.K.O", ".venv", "Scripts", "python.exe"),
            os.path.join(str(Path.home()), "N.E.K.O", ".venv", "Scripts", "python.exe"),
        ]
        for cand in candidates:
            try:
                if os.path.isfile(os.path.normpath(cand)):
                    return os.path.normpath(cand)
            except OSError:
                continue
        for name in ("python", "python3"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def _ensure_asr_bridge(self):
        """确保外部 ASR 桥常驻子进程存活（幂等）。不可用返回 None。"""
        proc = getattr(self, "_asr_bridge_proc", None)
        if proc is not None and proc.poll() is None:
            return proc
        # 冷却：启动失败/崩溃后 60s 内不反复拉起，避免日志风暴
        if time.time() - getattr(self, "_asr_bridge_at", 0.0) < 60.0:
            return None
        py = self._find_asr_python()
        if not py:
            _logger.warning("未找到可用的外部 Python（BILI_ASR_PYTHON 或 N.E.K.O venv），声音转写降级为空")
            self._asr_bridge_at = time.time()
            return None
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_bridge.py")
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [py, script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ASR 桥启动失败：%s", exc)
            proc = None
        self._asr_bridge_proc = proc
        self._asr_bridge_at = time.time()
        return proc

    def _asr_transcribe_via_bridge(self, path: str, lang: str = "zh") -> str | None:
        """经外部 ASR 桥转写 wav：返回文本（可能为空字符串）；失败返回 None。"""
        proc = self._ensure_asr_bridge()
        if proc is None:
            return None
        try:
            proc.stdin.write(json.dumps({"op": "transcribe", "path": path, "lang": lang}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("bridge closed")
            data = json.loads(line)
            if data.get("ok"):
                return data.get("text") or ""
            raise RuntimeError(data.get("error") or "asr error")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ASR 桥转写失败：%s", exc)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self._asr_bridge_proc = None
            self._asr_bridge_at = 0.0  # 立即允许下次拉起（冷却从失败时刻起算）
            return None

    def _whisper_model(self):
        """返回可用的转写 callable f(path, lang)->text；不可用返回 None。

        v0.7.14：N.E.K.O 打包运行时（projectneko_server.exe）不含 faster-whisper，
        改由外部 Python 常驻桥（asr_bridge.py）承担转写，进程内不再直接 import。
        """
        bridge = getattr(self, "_asr_bridge_fn", None)
        if bridge is None:
            bridge = self._asr_transcribe_via_bridge
            self._asr_bridge_fn = bridge
        return bridge

    def _kick_asr(self) -> None:
        """确保 ASR 后台 worker 线程在跑（幂等；startup 或首次收到声音段时启动）。"""
        with self._asr_lock():
            worker = getattr(self, "_asr_worker_thread", None)
            if worker is not None and worker.is_alive():
                return
            self._asr_worker_evt = threading.Event()
            self._asr_worker_stop = False
            worker = threading.Thread(target=self._asr_worker_loop, name="bili-asr", daemon=True)
            self._asr_worker_thread = worker
            worker.start()
            # v0.7.19：worker 启动即唤醒一次，处理可能已在队列里的声音段
            self._asr_worker_evt.set()

    def _asr_worker_loop(self) -> None:
        """后台 ASR 循环：有新声音段且空闲时转写，结果静默写入 _ext_asr_data。

        转写是 CPU 密集操作，串行排队；若上一段仍在转写，新段跳过（不积压，
        保证 YUI 听到的是最近内容）。全部异常都静默降级，绝不影响插件主流程。
        _asr_worker_stop 置真后线程在下个空闲点退出（插件关闭/测试用）。
        """
        model: object = None
        last_seq: int | None = None
        while not getattr(self, "_asr_worker_stop", False):
            try:
                if not self._asr_worker_evt.wait(timeout=1.0):
                    continue
                self._asr_worker_evt.clear()
                seg = self._ext_audio()
                if not seg or seg.get("seq") == last_seq:
                    continue
                try:
                    if model is None:
                        model = self._whisper_model()
                    text = ""
                    if model is not None:
                        text = (model(seg["path"], "zh") or "").strip()
                    if text:
                        self._ext_asr_data = {
                            "text": text[:500],
                            "lang": "zh",
                            "at": time.time(),
                            "seq": seg.get("seq"),
                        }
                        _logger.info("ext audio asr ok: seq=%s chars=%d", seg.get("seq"), len(text))
                    else:
                        _logger.info("ext audio asr empty: seq=%s（无声或噪音）", seg.get("seq"))
                except Exception as exc:
                    _logger.warning("ext audio asr failed: %s", exc)
                finally:
                    last_seq = seg.get("seq")
            except Exception:
                pass

    def _ext_asr_text(self) -> dict | None:
        """返回新鲜（30s 内）的最近 ASR 转写结果；无或过期返回 None。"""
        asr = getattr(self, "_ext_asr_data", None)
        if not asr or not asr.get("text"):
            return None
        if time.time() - float(asr.get("at") or 0) > _ASR_RESULT_WINDOW:
            return None
        return asr

    def _ext_tab(self) -> dict | None:
        """返回新鲜（120s 内）的扩展标签页数据；无或过期返回 None。

        v0.7.21：tab 推送带 source 后，优先返回与最近一次 cookie 来源（绑定浏览器）
        一致的标签页（避免 Chrome/Edge 多扩展并存时互相覆盖）；cookie 来源未知时
        返回最新 tab。
        """
        tab = getattr(self, "_ext_tab_data", None)
        if not tab:
            return None
        if time.time() - float(tab.get("at") or 0) > web_ui._EXT_ONLINE_WINDOW:
            return None
        src = getattr(self, "_ext_source", "") or ""
        tab_src = str(tab.get("source") or "")
        if src and tab_src and src != tab_src:
            return None
        return tab

    def _record_send_result(self, kind: str, ok: bool, error: str, message: str, target: str, dmid: str = "") -> None:
        """记录最近一次弹幕发送结果（供面板只读诊断；不含任何 cookie 内容）。"""
        self._last_send_result = {
            "kind": kind,
            "ok": bool(ok),
            "error": str(error or ""),
            "message": str(message or "")[:120],
            "target": str(target or ""),
            "dmid": str(dmid or ""),
            "at": int(time.time()),
        }
        _logger.info("danmaku send result: kind=%s ok=%s error=%s message=%s target=%s",
                     kind, ok, error, str(message or "")[:120], target)

    _DANMAKU_DEDUP_WINDOW = 60.0  # 同目标同内容弹幕的去重窗口（秒）

    def _dedup_lock(self) -> threading.Lock:
        # 惰性初始化：测试环境常用 object.__new__ 构造实例，__init__ 不会执行
        lock = getattr(self, "_danmaku_dedup_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._danmaku_dedup_lock = lock
        return lock

    def _danmaku_recently_sent(self, kind: str, target: str, text: str, progress: int) -> bool:
        """幂等去重：同一目标（房间/BV+进度）与内容在窗口内是否已发送过。"""
        key = f"{kind}|{target}|{progress}|{text}"
        with self._dedup_lock():
            sent = getattr(self, "_danmaku_sent", None)
            ts = (sent or {}).get(key)
            return ts is not None and (time.time() - ts) < self._DANMAKU_DEDUP_WINDOW

    def _mark_danmaku_sent(self, kind: str, target: str, text: str, progress: int) -> None:
        """记录一次真实发送时间戳；超过窗口的旧记录惰性清理。"""
        key = f"{kind}|{target}|{progress}|{text}"
        now = time.time()
        with self._dedup_lock():
            sent = getattr(self, "_danmaku_sent", None)
            if sent is None:
                sent = {}
                self._danmaku_sent = sent
            sent[key] = now
            stale = [k for k, ts in sent.items() if now - ts > self._DANMAKU_DEDUP_WINDOW * 2]
            for k in stale:
                sent.pop(k, None)

    def _panel_status(self) -> dict:
        """面板状态接口：浏览器接入状态 + B站绑定状态 + 扩展通道状态（纯本地，无网络）。

        复查优化（v0.7.3）：只读缓存 _browser_monitor（由 watcher 每 60s 刷新），
        不现场触发 CDP 探测——面板每 3s 轮询一次，若每次都探测会高频打调试端口。
        """
        cookie = os.environ.get("BILI_COOKIE", "") or ""
        source = getattr(self, "_cookie_source", _COOKIE_SOURCE_NONE)
        monitor = getattr(self, "_browser_monitor", None) or {"connected": False, "detail": "未探测", "at": 0}
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
            "last_send": getattr(self, "_last_send_result", None),
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
                dom_fn=self._receive_ext_dom,
                shot_fn=self._receive_ext_shot,
                audio_fn=self._receive_ext_audio,
                audio_state_fn=self._receive_ext_audio_state,
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
        # 浏览器接入监控：现场线程化刷新一次（8s 预算），失败沿用缓存；不重复同步探测
        monitor = getattr(self, "_browser_monitor", None) or {"connected": False, "detail": "未探测", "at": 0}
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
