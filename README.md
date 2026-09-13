# OneBot 协议端

这是一个 Windows 下运行的 Python OneBot v11 正向 WebSocket 客户端。它连接 go-cqhttp 或 NapCat 的正向 WebSocket Server，按分组监听源群，通过 QQ 原生合并转发或 QQ8 风格长图发送到一个或多个目标群。

## 功能

- 每个分组配置一个源群、多个目标群、独立模板和独立批量条数。
- 普通消息累计到 `batch_size` 后按原顺序发送；每组可选择 `forward_mode: "image"`（长图）或 `"forward"`（QQ 原生合并转发）。
- 灰条消息、戳一戳不计入批量条数，但会保留在消息顺序中；只在普通消息达到阈值时随批次发送。
- 图片、QQ 表情、引用、艾特昵称、语音波形、视频占位、文件占位和群头衔均支持渲染。
- 目标群支持 `/help`、`/ms <count>`、`/qz <count>`、`/status`。
- `/ms <count>` 查询源群最近消息并按本组转发方式发送，不会发送 count 条独立消息。
- 支持接收 QQ 合并转发消息并读取其中的节点；QQ 动态需额外 SDK 或 bridge，支持扩展事件和定时查询。
- 合并转发保留作者、时间、文字与图片，支持嵌套；读取失败或超过 5 层 / 200 节点时显示明确提示。原生合并转发内的文件显示链接，文件本体通过后端下载后另行上传到目标群。
- WebUI 提供分组增删改、多个目标群、后端选择、转发方式、批量阈值、动态监听 QQ 和轮询间隔设置。

## 配置

复制 `config.example.json` 或直接启动程序。旧版只有 `source_group_id` / `target_group_id` 的配置会自动迁移为 `groups` 格式。

```json
{
  "backend": "auto",
  "snowluma_ws_url": "ws://127.0.0.1:8095",
  "web_port": 8000,
  "groups": [
    {
      "id": "default",
      "name": "默认分组",
      "source_group_id": 0,
      "target_group_ids": [],
      "forward_template": "来自 {source_group_id} 的新消息：\\n发送者：{sender_nickname}（{sender_id}）\\n发送时间：{time}\\n{message}",
      "batch_size": 1,
      "forward_mode": "forward",
      "watched_qq_ids": [],
      "qz_poll_interval": 30
    }
  ]
}
```

`backend` 可选 `auto`（默认，连接后通过 `get_version_info` 识别）、`go-cqhttp`、`napcat`。`snowluma_ws_url` 保留旧字段名，适用于两种后端。旧配置未设置 `forward_mode` 时仍使用 `image`；新示例配置使用 `forward`。

`batch_size` 范围为 `1~50`。`watched_qq_ids` 是需要监听 QQ 动态的 QQ 号列表，`qz_poll_interval` 范围为 `5~3600` 秒。

启动前设置与后端一致的访问令牌（优先读取 `ONEBOT_ACCESS_TOKEN`，未设置时兼容 `SNOWLUMA_ACCESS_TOKEN`）：

```powershell
$env:ONEBOT_ACCESS_TOKEN="替换为后端的 access-token"
.\venv\Scripts\python.exe .\main.py
```

WebUI 地址：`http://127.0.0.1:8000`。

## go-cqhttp 配置

在 go-cqhttp 的 `config.yml` 中启用正向 WebSocket，例如：

```yaml
message:
  post-format: array
servers:
  - ws:
      address: 127.0.0.1:8095
      middlewares:
        access-token: "替换为自己的令牌"
```

把这些字段合入现有配置，不要覆盖账号等其他配置。转发器填写 `ws://127.0.0.1:8095/`：`/` 同时收事件和调用 API，`/api` 只支持 API、`/event` 只支持事件，因此不能用于本程序。支持事件中的数组消息与 CQ 码字符串消息。

go-cqhttp 的 `get_group_msg_history` 每次返回最多 20 条，以 `message_seq` 向前翻页；`/ms` 会按需分页并去重。合并转发通过 `get_forward_msg` 读取，原生发送使用 `send_group_forward_msg` 与 `node` 节点。

官方文档：[通信方式](https://docs.go-cqhttp.org/guide/communication.html)、[扩展 API](https://docs.go-cqhttp.org/api/)、[CQ 码与消息段](https://docs.go-cqhttp.org/cqcode/)。

## 命令

- `/help`：显示帮助。
- `/ms 10`：查询本分组源群最近 10 条消息并按本组配置发送为长图或原生合并转发。
- `/qz 10`：查询本分组监听列表最近 10 条 QQ 动态，按本组转发方式发送。
- `/status`：显示连接、分组和当前批量状态。

## QQ 动态前置条件

QQ 空间动态不是 OneBot v11 标准能力。当前后端默认使用 PyPI 的 `qzone-sdk`：它通过当前 OneBot WebSocket 的标准 `get_cookies` 与 `get_login_info` 动作取得登录态，再直接查询指定 QQ 的说说。依赖安装：

```powershell
.\venv\Scripts\python.exe -m pip install "qzone-sdk[napcat]"
```

如果安装了 `onebot-qzone` bridge，也可以在分组的 `qz_action` 中选择 `get_emotion_list` 或 `get_friend_feeds` 作为回退动作。go-cqhttp 和 NapCat 原生均不提供这些 Qzone 动作；没有 SDK 登录态或 bridge 时，`/qz` 会提示动态不可用，并暂停重复探测。

## 运行检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/status | ConvertTo-Json -Depth 10
netstat -ano | findstr :8000
netstat -ano | findstr :8095
```

`/api/status` 中 `connected: true` 表示后端已连接 OneBot WebSocket；QQ 登录状态仍需在对应后端确认。
