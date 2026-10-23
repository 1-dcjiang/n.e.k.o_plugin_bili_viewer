# B站全能助手（bili_viewer）

让 YUI 浏览、感知并管理哔哩哔哩的 N.E.K.O 插件：搜索视频/直播/用户，查看视频详情、弹幕、CC 字幕、AI 概要、评论，直播间信息与实时弹幕，UP主信息与投稿列表；**感知用户当前正在看/用的内容**（前台窗口通用感知），并支持管理自己的稿件、直播时回复弹幕。

## 功能

| 工具 | 说明 |
|---|---|
| `bili_search` | 搜索视频 / 直播 / 用户 |
| `bili_video_info` | 视频详情：标题、UP主、时长、分区、播放/弹幕/点赞/投币/收藏、分P、字幕、标签 |
| `bili_video_danmaku` | 视频弹幕文本（带时间点） |
| `bili_video_subtitle` | 视频 CC 字幕逐句文本 |
| `bili_video_ai_summary` | B站官方 AI 视频概要（部分视频提供） |
| `bili_video_comments` | 视频评论（热门/最新、分页） |
| `bili_live_info` | 直播间信息：标题、主播、状态、在线人数、分区、简介 |
| `bili_live_danmaku` | 直播间实时弹幕（匿名采集数秒） |
| `bili_user_info` | UP主信息：粉丝、关注、视频数、等级、签名 |
| `bili_user_videos` | UP主的投稿视频列表 |
| `bili_now_watching` | **感知当前正在看/用的内容**：识别前台窗口的应用/站点/页面主题；B站视频/直播页自动匹配详情 |
| `bili_my_info` | 当前账号（BILI_COOKIE）信息：昵称、uid、等级、硬币、大会员 |
| `bili_my_videos` | 自己账号的稿件列表（状态/播放/时长） |
| `bili_edit_video` | 修改自己稿件的标题/简介/标签（需登录态，写操作） |
| `bili_live_send_danmaku` | 向直播间发送弹幕（直播时回复观众，需登录态，写操作） |
| `bili_inbox` | **收件箱**：未读数 + 新的回复/@/赞/系统通知/未读私信一次看完（读取后 B站 自动标已读） |
| `bili_unread` / `bili_feed_replies` / `bili_feed_mentions` / `bili_feed_likes` / `bili_feed_system` | 未读数 / 评论回复 / @我的 / 收到的赞 / 系统通知 |
| `bili_dm_list` / `bili_dm_read` / `bili_dm_send` | 私信会话列表 / 读私信（可标记已读）/ 发私信（白名单：主账号、对方先发过、互关；≤500 字；回读验证） |
| `bili_video_relation` / `bili_video_like` / `bili_video_coin` / `bili_video_fav` / `bili_video_triple` | 我与视频关系 / 点赞 / 投币（每视频≤2、每日≤5）/ 收藏 / 一键三连——全部回读验证防假成功 |
| `bili_my_favs` | 我的收藏夹列表 |
| `bili_rank` | 热门榜单：分区榜 / 每周必看 / 入站必刷 |
| `bili_my_stats` | 创作数据：累计播放/互动/粉丝 + 最近稿件概览 |

> v0.8.0 融合开源项目 bili-cli（github.com/kukemc/bili-cli）精华：收件箱、私信白名单护栏、
> 表态类操作（点赞/投币/收藏/三连，投币每日 5 币上限）、本地限流 + 审计日志 + B站 错误码分类提示。
> 纯同步 urllib 实现，不引入 bilibili-api 等重依赖，完全兼容既有架构。
> v0.8.1 复查修复：私信读/发接口按官方定义补 wbi 签名（wts+w_rid，防 -412 风控）；
> 创作中心接口对齐 verify-only；未读私信先过滤再截取；标记已读补 csrf。
> v0.8.2 关注错人修复：target 支持 owner/main/主人/主账号/大号 = 主账号（零歧义，绝不按昵称搜索）；
> 此前 YUI 传 owner 曾按昵称搜到真名 owner 的陌生账号，现已根治。
> v0.8.3 自主查看私信：后台每 3 分钟轮询小号未读（私信/回复/@/赞/系统），有未读且 20 分钟
> 冷却已过时静默注入 YUI 上下文，YUI 自主查看并酌情回复（bili_inbox/bili_dm_read/bili_dm_send）。
> v0.8.4 私信漏报修复：B站 网页打开会话后 unread_count/msgfeed.chat 归 0（新消息仍真实存在），
> 已改为本地已读游标按 msg_seqno 增量判定新私信（dm_seen.json），未读/收件箱/自主轮询同口径。
> v0.8.5 弹幕@自主回复：直播间有人@小号时，扩展采集携带房间号（room），
> 感知循环静默注入自主回复任务（bili_live_send_danmaku account=yui）；同一直播间 10 分钟冷却防刷屏，
> 无房间号或冷却中只提醒不催回复。
> v0.8.6 直播间活跃观众：新增 bili_live_viewers 工具——按房间读取最近弹幕发言者并尽力解析 uid，
> 解析不出的绝不猜 uid 关注（防关注错人）；B站 无公开 HTTP 在线观众列表（页面数字来自 WS 实时流），
> 接口 online 实为人气值，已统一展示为"人气：N"并加直播中 online=0 的兜底（防"在线0人"误报）。
> 同时修复隐藏 bug：感知循环/工具曾用顶层 `import web_ui` 加载出第二份模块实例，
> 与扩展 POST 写入的弹幕缓冲分裂——弹幕/观众因此一直读不到，现已全部统一为包内引用。

## 使用示例（对 YUI 说）

- "搜一下B站：原神 4.0 剧情" → `bili_search(keyword="原神 4.0 剧情")`
- "看看 BV1xx411c7mD 这个视频" → `bili_video_info(bvid="BV1xx411c7mD")`
- "这个视频弹幕在刷什么" → `bili_video_danmaku(bvid="...")`
- "帮我总结一下这个视频" → `bili_video_ai_summary(bvid="...")`
- "看看评论怎么说" → `bili_video_comments(bvid="...", mode="hot")`
- "xxx 的直播间开着吗，多少人在线" → `bili_live_info(room="12345")`
- "直播间现在弹幕在说什么" → `bili_live_danmaku(room="12345", duration=8)`
- "查一下这个UP主" → `bili_user_info(mid=12345)` / `bili_user_videos(mid=12345)`
- "我现在在看什么 / 我切视频了 / 跟着我看" → `bili_now_watching()`（或直接靠实时同步，YUI 自动知道）
- "看看我的账号/我的稿件" → `bili_my_info()` / `bili_my_videos()`
- "把我的视频 BV1xx... 标题改成 XXX" → `bili_edit_video(bvid="...", title="XXX")`
- "帮我在直播间回复弹幕：谢谢大家的关注" → `bili_live_send_danmaku(room="12345", text="谢谢大家的关注")`

## 感知当前观看（bili_now_watching + 实时同步）

- **实时同步（默认开启）**：插件后台每 2.5 秒感知一次你的观看内容，检测到变化（切视频/切网页/切应用）后，把"用户当前正在看/用 X"静默注入 YUI 的上下文——**YUI 下一条回复会自动知道你正在看什么**，无需你开口。注入为 `read` 模式：不打断、不刷屏（内容稳定 2 次才推、同一内容只推一次）。
- **多窗口感知**：不再只看前台窗口——枚举所有可见窗口，**排除 N.E.K.O 聊天客户端自身与开发终端**（Terminal/PowerShell/cmd），按"B站视频/直播 > 已知站点 > 网页 > 应用"选你最可能在看的内容。所以你切回 N.E.K.O 和 YUI 打字时，她依然知道你后台开着哪个 B站视频。
- 推送文本标注"前台窗口推断"，YUI 会将其作为线索而非事实。
- 前台是 **B站视频页** 时，`bili_now_watching` 会搜索匹配并返回详情（标题/UP主/播放/弹幕/时长/BV号/简介）；**直播页** 返回直播主题。
- 可关闭实时同步：设置环境变量 `BILLI_SYNC_DISABLE=1` 后重启客户端。
- 仅读取窗口标题与进程名（本地操作，无网络传输、不读取页面内容）
- 平台：Windows（读取前台窗口）；其他平台自动停用同步

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
- 直播弹幕为匿名游客采集，不做刷屏/干扰；发送弹幕限长 30 字
- 网络受限或触发风控时返回明确提示，不重试轰炸
- 纯标准库实现（urllib + socket + ssl + zlib + gzip + ctypes），无第三方依赖

## 内容"看"的边界

YUI 无法解码视频画面，但可通过 **字幕 + 弹幕 + AI 概要 + 简介/评论** 充分理解视频内容与观众反应；直播通过 **直播间信息 + 实时弹幕** 掌握现场动态，并可用发送弹幕与观众互动。

## 开发与构建

```bash
python -m pytest plugin/plugins/bili_viewer/tests/ -q
python -m plugin.neko_plugin_cli.cli check -r bili_viewer
```
