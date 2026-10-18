# B站看板（bili_viewer）

让 YUI 浏览哔哩哔哩的 N.E.K.O 插件：搜索视频/直播/用户，查看视频详情、弹幕、CC 字幕、AI 概要、评论，直播间信息与实时弹幕，UP主信息与投稿列表，并支持感知用户当前正在看/用的内容（前台窗口通用感知）与管理自己的稿件、在直播时回复弹幕。

## 功能

| 工具 | 说明 |
|---|---|
| `bili_search` | 搜索视频 / 直播 / 用户，返回候选列表 |
| `bili_video_info` | 视频详情：标题、UP主、时长、分区、播放/弹幕/点赞/投币/收藏、分P、字幕、标签 |
| `bili_video_danmaku` | 视频弹幕文本（带时间点），了解观众反应 |
| `bili_video_subtitle` | 视频 CC 字幕逐句文本，快速通读内容 |
| `bili_video_ai_summary` | B站官方 AI 视频概要（部分视频提供） |
| `bili_video_comments` | 视频评论（热门/最新、分页） |
| `bili_live_info` | 直播间信息：标题、主播、状态、在线人数、分区、简介 |
| `bili_live_danmaku` | 直播间实时弹幕（匿名连接弹幕服务器采集数秒） |
| `bili_user_info` | UP主信息：粉丝、关注、视频数、等级、签名 |
| `bili_user_videos` | UP主的投稿视频列表 |
| `bili_now_watching` | 感知用户当前正在看/用的内容：识别前台窗口的应用/站点/页面主题；B站视频/直播页自动匹配详情 |
| `bili_my_info` | 当前账号（BILI_COOKIE）信息：昵称、uid、等级、硬币、大会员 |
| `bili_my_videos` | 自己账号的稿件列表（状态/播放/时长） |
| `bili_edit_video` | 修改自己稿件的标题/简介/标签（需登录态，写操作） |
| `bili_live_send_danmaku` | 向直播间发送弹幕（直播时回复观众，需登录态，写操作） |

## 使用示例（对 YUI 说）

- "搜一下B站：原神 4.0 剧情" → `bili_search(keyword="原神 4.0 剧情")`
- "看看 BV1xx411c7mD 这个视频" → `bili_video_info(bvid="BV1xx411c7mD")`
- "这个视频弹幕在刷什么" → `bili_video_danmaku(bvid="...")`
- "帮我总结一下这个视频" → `bili_video_ai_summary(bvid="...")`（无 AI 概要时可用字幕或简介）
- "看看评论怎么说" → `bili_video_comments(bvid="...", mode="hot")`
- "xxx 的直播间开着吗，多少人在线" → `bili_live_info(room="12345")`
- "直播间现在弹幕在说什么" → `bili_live_danmaku(room="12345", duration=8)`
- "查一下这个UP主" → `bili_user_info(mid=12345)` / `bili_user_videos(mid=12345)`
- "我现在在看什么 / 我切视频了 / 跟着我看" → `bili_now_watching()`
- "看看我的账号/我的稿件" → `bili_my_info()` / `bili_my_videos()`
- "把我的视频 BV1xx... 标题改成 XXX" → `bili_edit_video(bvid="...", title="XXX")`
- "帮我在直播间回复弹幕：谢谢大家的关注" → `bili_live_send_danmaku(room="12345", text="谢谢大家的关注")`

## 感知当前观看（bili_now_watching）

- 读取当前前台窗口的标题与进程，识别应用（chrome/steam/微信/游戏 exe 等）、站点（B站/知乎/微博/小红书/YouTube/贴吧/淘宝/京东/GitHub 等 18 类）、页面/窗口主题
- 前台为 B站视频页时自动搜索匹配并返回视频详情；直播页返回直播主题
- 仅读取窗口标题与进程名（本地操作，无网络传输、不读取页面内容）
- 平台：Windows；其他平台返回明确提示

## 配置（可选）

环境变量 `BILI_COOKIE`：完整的 B站 Cookie 字符串（含 `SESSDATA=...`）。
不配置时搜索、视频详情、评论、字幕、AI 概要、直播信息、UP主信息均可用；
**视频弹幕与直播实时弹幕**在游客模式下可能被 B站加扰/风控（接口返回加密数据），
配置 BILI_COOKIE 后可正常获取。
**稿件管理与直播发弹幕**为写操作，必须配置 BILI_COOKIE（含 `bili_jct`），
且只作用于当前账号自己的内容，请仅在明确确认后使用。
插件绝不把 Cookie 写入磁盘或对外发送。

## 安全说明

- 读操作只访问公开内容；写操作（改稿件、发弹幕）仅限当前账号自己的内容，需登录态与用户明确确认
- 感知功能仅读取前台窗口标题与进程名（本地、无网络传输）
- 直播弹幕采集为匿名游客方式，不做刷屏/干扰；发送弹幕限长 30 字
- 网络受限或触发风控时返回明确提示，不重试轰炸
- 纯标准库实现（urllib + socket + ssl + zlib + gzip + ctypes），无第三方依赖

## 内容"看"的边界

YUI 无法解码视频画面，但可通过 **字幕 + 弹幕 + AI 概要 + 简介/评论** 充分理解视频内容与观众反应；直播通过 **直播间信息 + 实时弹幕** 掌握现场动态，并可用发送弹幕与观众互动。

## 开发与构建

```bash
python -m pytest plugin/plugins/bili_viewer/tests/ -q
python -m plugin.neko_plugin_cli.cli check -r bili_viewer
```
