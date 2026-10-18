# B站全能助手（bili_viewer）

让 YUI 浏览、感知并管理哔哩哔哩的 N.E.K.O 插件：搜索视频/直播/用户，查看视频详情、弹幕、CC 字幕、AI 概要、评论，直播间信息与实时弹幕，UP主信息与投稿列表，感知用户当前正在看/用的内容（前台窗口通用感知），并支持管理自己的稿件、在直播时回复弹幕。

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

## 感知当前观看（bili_now_watching + 实时同步）

- **实时同步（默认开启）**：插件后台每 2.5 秒感知一次观看内容，检测到变化（切视频/切网页/切应用）后，把"用户当前正在看/用 X"推送注入 YUI 上下文——**B站内容主动触发 YUI 开口（自动跟上），其余静默注入**，无需你开口。内容稳定 2 次才推、同一内容只推一次、两次推送间隔 ≥15 秒（防刷屏）。
- **多窗口感知**：枚举所有可见窗口，排除 N.E.K.O 聊天客户端自身与开发终端（Terminal/PowerShell/cmd），按"B站视频/直播 > 已知站点 > 网页 > 应用"选最可能在看的内容。切回 N.E.K.O 打字时，YUI 依然知道你后台开着哪个 B站视频。
- 推送文本标注"实时感知"，YUI 将其作为线索而非事实。
- 前台为 B站视频页时 `bili_now_watching` 自动搜索匹配并返回视频详情；直播页返回直播主题
- 可关闭实时同步：环境变量 `BILLI_SYNC_DISABLE=1` 后重启客户端
- 仅读取窗口标题与进程名（本地操作，无网络传输、不读取页面内容）
- 平台：Windows；其他平台自动停用同步

## 配置（可选）——接入你的 B站账号

插件提供两种方式接入账号，让被 B站对游客风控/拦截的接口（直播弹幕 -352 等）正常使用：

### 方式一：自动绑定（推荐，无需手动复制）

1. 先在浏览器里登录一次 bilibili.com（扫码或账号密码均可）；
2. 对 YUI 说"**允许你读取浏览器 Cookie 绑定 B站账号**"——YUI 会调用 `bili_bind_account(allow=true)` 记录授权（只确认一次，永久生效）；
3. **关闭 Chrome/Edge**（浏览器运行中 Cookie 文件被独占锁定，插件读不到），然后重启 N.E.K.O 客户端；
4. 插件自动读取浏览器中已登录的 B站 Cookie 并绑定。可让 YUI 用 `bili_bind_status` 查看绑定状态。

读取为本地操作：Cookie 不上传、不写日志、不发给 AI。

### 方式二：手动配置（备选）

在客户端插件管理页的 bili_viewer 配置界面填写 `bili_cookie`；
或编辑 `C:\Users\jh\AppData\Local\N.E.K.O\.neko-package-profiles\bili_viewer\default.toml`，
在文件末尾追加：

```toml
[settings]
bili_cookie = "完整Cookie字符串"
```

获取方法：浏览器登录 bilibili.com → F12 → Application（应用）→ Cookies → `https://www.bilibili.com` → 任选一条 Cookie 右键复制。
要求：读接口需含 `SESSDATA`；写接口（发弹幕/改稿件）还需 `bili_jct`。手动配置优先级高于自动读取。
插件绝不把 Cookie 写入日志或对外发送。

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
