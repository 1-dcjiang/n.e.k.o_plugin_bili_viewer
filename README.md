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
> v0.8.7 直播定位纠偏（修复"直播间定位错误"）：弹幕 WS 流绑定的房间号=页面真实直播间
> （弹幕一来就刷新，一直新鲜），而扩展标签页事件只在切换/加载标签时推送一次——长时间
> 停留会过期，过期后感知回退窗口标题搜索（标题无房间号，按主播名搜索可能定位到该主播
> 的旧房间/同名直播间）。现在：标签页过期时用弹幕房间兜底，标签页直播房间与弹幕房间
> 冲突时以弹幕房间为准；tab 接收也补了本地诊断日志（仅 URL/标题，无 cookie）。
> v0.8.8 动态发布接口修复（修复"怎么发布失败了"）：原 `/x/dynamic/web/create/v1/dyn`
> 已被 B站 下线（HTTP 404），改用旧版稳定接口 `api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create`
> （body: type=4, content=JSON字符串, from=create.client.web, api=req），返回 dynamic_id；
> 新增 `delete_dynamic()`（rm_dynamic 接口，供误发撤回/回读验证）；工具层补错误码日志，
> 以后动态失败可直接查日志对线。实测：cookie 有效、wbi 正常、旧接口发+删均 code=0。
> v0.8.9 视频详情支持操作账号 cookie（修复"查询视频并点赞显示无权限访问"）：`video_info`
> 写死用主账号 cookie，主账号失效时 `/x/web-interface/view` 返回 -403，而点赞/投币/收藏/
> 评论都先经 `_bvid_aid` → `video_info`，导致整条操作链路挂。现在 `video_info(bvid, account=...)`
> 支持账号参数，操作链路用当前操作账号（默认 yui 小号）的 cookie 查详情；`bili_video_info`
> 工具也加 account 参数（默认 yui）。
> v0.9.0 私信自主检测直读内容（修复"不知道你发了什么/说回复了但没发"）：`_unread_tick`
> 之前只注入"有未读私信"的提醒，YUI 收到后不一定主动调 `bili_dm_read` 读内容，导致
> "没注意看内容"。现在检测到私信未读时**直接读取最新私信内容注入**（优先主账号会话，
> 失败降级为原提醒），YUI 一眼就能看到对方什么时候说了什么；`bili_dm_read` 输出从
> 原始 JSON 改为易读文本（"[时间] 对方/我：内容"），LLM 友好。私信发送仍有回读验证
> （`_dm_verified`），假成功抛 `DM_UNVERIFIED`，严禁声称已发送。
> v0.9.1 修复私信查询工具路由（修复"YUI说没有你的消息"）：日志实证 YUI 查私信时
> TaskExecutor 误选 `run` 通用入口（`entry=run, action=query`），但 `run` 入口的 `action=query`
> **根本没有实现私信查询**——走到兜底返回"用户当前正在看「xxx页面」"，YUI 基于错误结果
> 说"没有你的消息"。修复：(1) `run` 入口 description 明确排除私信/收件箱查询，引导走
> `bili_dm_read`/`bili_dm_list`/`bili_inbox` 专用工具；(2) input_schema 去掉 `query` 选项；
> (3) `entry_run` 增加 `action=query` 分支（只做页面感知，不做私信查询）；
> (4) `bili_dm_read` description 加强触发词（"你看到我的私信了吗/我发的你能看到吗"）。
> v0.9.2 修复私信发送假失败（修复"YUI说发不出去但实际发了两条"）：日志+截图实证
> YUI 调 `bili_dm_send` 发私信，接口实际发送成功（B站网页能看到"喵~看到你的私信啦！"），
> 但 `_dm_verified` 回读验证返回 False（B站 `fetch_session_msgs` 接口同步有延迟，原 tries=2
> 间隔 1.2s 总共等 2.4s 不够），抛 `DM_UNVERIFIED`，YUI 基于错误结果说"发不出去"并重试，
> 导致重复发送（对方收到两条相同私信）。修复：(1) `_dm_verified` tries 从 2 增到 3、间隔
> 从 1.2s 增到 1.5s（总等待从 2.4s 增到 4.5s）；(2) `DM_UNVERIFIED` 错误消息明确说"私信
> 可能已经发出去了，严禁重复发送"；(3) `bili_dm_send` description 加 `DM_UNVERIFIED` 处理
> 铁律（返回此错误时不要重试，回复用户'私信可能已发出，请在 B站 私信页面确认'）。
> v0.9.3 修复动态内容被包成 JSON（修复"动态发出来的东西很奇怪"）：截图实证 YUI 小号
> 发的动态显示为 `{"content": "本喵是YUI..."}` 而非纯文本。根因：`post_dynamic` 里
> `content` 被包成了 `json.dumps({"content": text})`，但 B站 vc 接口的 `content` 字段
> 应该是纯文本；接口返回 code=0（假成功），但实际把整个 JSON 字符串当成了动态内容。
> 修复：(1) `content` 改用纯文本 `text`；(2) 新增 `_dynamic_verified` 回读验证——发布后
> 调动态详情接口确认内容正确，不匹配则自动删除误发动态并抛 `DYN_UNVERIFIED`（防假成功）；
> (3) 新增 `bili_dynamic_delete` 工具（YUI 说"不知道怎么删动态"就是因为没这个工具）。
> v0.9.4 修复私信自动检测与自动回复（修复"还是不会自动检测私信，检测到也不会自动回复"）：
> 根因有三：(1) `_unread_tick` 注入用 `ai_behavior="read"`（只读不触发回复），YUI 收到私信
> 提醒后只是"读了"但不回复——N.E.K.O `push_message` 支持 `respond|read|blind`，私信必须用
> `respond` 才会触发 YUI 主动回复；(2) 冷却时间 20 分钟太长，私信需及时回复；(3) 注入私信
> 内容后没有更新本地已读游标（`dm_seen.json`），5 分钟后 `_unread_tick` 又检测到同一条私信
> 是"新消息"，重复注入导致 YUI 重复回复。修复：(1) 私信未读注入用 `ai_behavior="respond"`
> 触发回复，其他未读（回复/@/赞/系统）保持 `read`；(2) 冷却从 20 分钟缩短到 5 分钟；
> (3) `_read_latest_dm_for_inject` 读取后立即调用 `record_dm_seen` 更新游标，防重复注入。
> v0.9.5 修复动态发布假失败（修复"让她发一次，她发了3次"）：与 v0.9.2 私信假失败同一模式——
> `_dynamic_verified` 只重试 2 次间隔 1 秒，B站动态详情接口同步有延迟，发布成功但回读未确认
> → 抛 `DYN_UNVERIFIED` → YUI 认为失败重试 → 重复发布。修复：(1) `_dynamic_verified` tries 2→3，
> 间隔 1.0→1.5s；(2) 内容不匹配时也重试一次（可能是接口延迟返回旧缓存），不是直接判定失败；
> (3) `DYN_UNVERIFIED` 错误消息明确"动态可能已成功发出，严禁重复发送"；
> (4) `bili_yui_dynamic` description 加 `DYN_UNVERIFIED` 处理铁律。
> v0.9.6 全盘复查：修复 4 处 account 路由问题（写操作链路里 video_info/回读验证默认用 main cookie，
> main 失效时导致假失败）：(1) `_wbi_get` 加 account 参数（默认 main 向后兼容）；
> (2) `_reply_visible` 加 account 参数，回读评论用发评论的账号 cookie；
> (3) `send_video_danmaku` 里 `video_info` 传 account；(4) `edit_video` 里 `video_info` 传 account。
> 同步更新 8 个测试 mock 函数签名。

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
