# go-cqhttp 后端与合并转发兼容记录

核对日期：2026-09-13。本文仅记录官方接口与实现事实，以及由此得到的适配建议；不代表已完成真实 QQ 账号联调。

## 来源与版本

go-cqhttp 主仓库的 [docs/README.md](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/docs/README.md) 指定官方文档站为 <https://docs.go-cqhttp.org>，文档源仓库为 [ishkong/go-cqhttp-docs](https://github.com/ishkong/go-cqhttp-docs)。本次读取该文档仓库 main，并以 go-cqhttp 源码提交 `a5923f179b360331786a6509eb33481e775a7bd1` 核对文档中存在的历史差异。不同发布版支持能力可能不同。

## 连接、请求与事件

- 正向 WebSocket `/` 同时承载事件和 API；`/api` 仅用于 API，`/event` 仅用于事件。客户端转发器若用同一连接收发，应连接 `/`。[API 文档](https://docs.go-cqhttp.org/api/)、[WebSocket 源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/server/websocket.go)
- API 请求形状为 `{"action":"send_group_msg","params":{...},"echo":"unique-id"}`；应使用唯一 echo 将响应与请求匹配。成功响应为 `status: "ok"`、`retcode: 0`、`data`；失败信息可能位于 `msg`、`wording`、`message`。不能仅将“收到 JSON”视作发送成功。[API 文档](https://docs.go-cqhttp.org/api/)、[OK/Failed 实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 正向连接鉴权支持 `Authorization` 头或 `access_token` 查询参数，源码会取 Authorization 第一个空格后的值，因此 Bearer 格式可用。go-cqhttp 作为反向 WebSocket 客户端时发送 `Authorization: Token ...`。[HTTP 鉴权源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/server/http.go)、[WebSocket 源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/server/websocket.go)
- `message.post-format` 可为 `string` 或 `array`，官方默认配置为 `string`。`message` 与合并转发内容都不能假定始终为消息段数组。[配置文档](https://docs.go-cqhttp.org/guide/config.html)、[ToFormattedMessage 与合并转发实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 消息事件 `post_type=message`，`message_type=group/private`；`message_sent` 是自身发出的消息，仅在 `report-self-message` 开启时上报。心跳和生命周期属于 `meta_event`，不应作为聊天消息转发。[事件文档](https://docs.go-cqhttp.org/event/)

## 合并转发读取

接收的消息段为 `{"type":"forward","data":{"id":"资源ID"}}`，CQ 字符串等价为 `[CQ:forward,id=资源ID]`。这里的 ID 是合并转发资源 ID，不是外层事件的整数消息 ID。[CQ 文档](https://docs.go-cqhttp.org/cqcode/)

官方文档规定调用：

```json
{"action":"get_forward_msg","params":{"message_id":"资源ID"},"echo":"forward-1"}
```

响应 `data.messages` 中每条记录形如：

```json
{
  "sender": {"user_id": 10086, "nickname": "发送者"},
  "time": 1595694374,
  "content": "文字[CQ:image,file=缓存名,url=https://example.org/image.png]"
}
```

`content` 也可为普通消息段数组。当前源码额外返回 `group_id`，并接受 `message_id` 与 `id` 两种参数名，优先 `message_id`。[API 文档](https://docs.go-cqhttp.org/api/)、[API 路由源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/modules/api/api.go)

特别注意：当前 `CQGetForwardMessage` 在节点内容只包含一个嵌套 `ForwardMessage` 时，将 `content` 直接设为下一层 `[{sender,time,content,group_id}, ...]`。这不是 `[{type,data}, ...]` 消息段数组。适配层应识别三种内容：CQ 字符串、普通消息段数组、原始合并记录数组，再递归归一化，避免把嵌套消息变成空文本。[CQGetForwardMessage 实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)

## 合并转发发送

| 目标 | action | 参数 |
| --- | --- | --- |
| 群聊 | `send_group_forward_msg` | `group_id`, `messages` |
| 好友私聊 | `send_private_forward_msg` | `user_id`, `messages` |

两者成功返回 `data.message_id` 与 `data.forward_id`。`messages` 应传 JSON node 数组，不是普通 message 参数，也不能使用不支持嵌套结构的 HTTP GET/form 调用。[API 文档](https://docs.go-cqhttp.org/api/)

自定义节点的官方文档形式：

```json
{
  "type": "node",
  "data": {
    "name": "原发送者",
    "uin": "10086",
    "time": 1595694374,
    "content": [
      {"type":"text","data":{"text":"原始内容"}}
    ]
  }
}
```

- 文档字段为 `name`、`uin`、`content`；源码也支持 `nickname` 和 `user_id` 别名。源码对非嵌套自定义节点要求非零用户 ID、非空名字和非空内容，否则跳过该节点。[CQ 文档](https://docs.go-cqhttp.org/cqcode/)、[uploadForwardElement 实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 另一种节点为 `{"type":"node","data":{"id":"已有消息ID"}}`，引用后端消息数据库中的消息；不要把 forward 资源 ID 填到这里。源码在数据库未启用或找不到引用时跳过该节点。[CQ 文档](https://docs.go-cqhttp.org/cqcode/)、[uploadForwardElement 实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 文档写自定义内容“不支持转发套娃”，但当前源码已支持 `content` 内包含 `type: node` 的数组，并递归构建合并转发。适配应保留递归结构，同时避免无限递归/循环资源引用。普通段与 node 混合时，源码一旦发现 node 就按嵌套分支处理，不能直接混入文字并假定全部保留。[文档](https://docs.go-cqhttp.org/cqcode/)、[uploadForwardElement 实现](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 文档将 `forward` 标为仅接收，但当前 `cqcode.go` 的 `case "forward"` 会根据 ID 下载资源，因此部分版本可以直接重发资源。为保留节点作者与处理跨实现差异，可使用“取内容 → 规范化 → node API”路径，不能依据文档断言所有版本都禁止重发 forward。[文档](https://docs.go-cqhttp.org/cqcode/)、[CQ 转换源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/cqcode.go)

## CQ 字符串解析

CQ 参数的转义包括 `&amp;`、`&#91;`、`&#93;`、`&#44;`；普通文本只有前三项，普通文本中的 `&#44;` 不应被当作参数转义解码。应先在原始文本中识别 CQ 边界、分隔参数，然后反转义，且最后替换 `&amp;`，防止二次解码。例如文本 `&amp;#91;` 应还原为字面量 `&#91;`，不是 `[`。JSON 消息段的 data 已经是结构化内容，不应再次套用 CQ 反转义。[CQ 文档](https://docs.go-cqhttp.org/cqcode/)、[官方 EscapeText/UnescapeText/UnescapeValue](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/internal/msg/element.go)、[官方解析器](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/internal/msg/parse.go)

## 媒体和文件

- 图片段 `data.file` 可能是 go-cqhttp 缓存名，`data.url` 是下载地址；`get_image` 参数为 `file`，文档返回 `size`、`filename`、`url`，源码另外返回后端本机缓存路径 `file`。跨机器部署应优先 URL，不应假定后端路径在转发器机器上存在。[CQ 文档](https://docs.go-cqhttp.org/cqcode/)、[API 文档](https://docs.go-cqhttp.org/api/)、[CQGetImage 源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/coolq/api.go)
- 群文件通过 `notice_type=group_upload` 上报，`file` 为 `{id,name,size,busid}`；可调用 `get_group_file_url`，参数 `group_id`、`file_id`、`busid`，获取 `data.url`。[事件文档](https://docs.go-cqhttp.org/event/)、[API 文档](https://docs.go-cqhttp.org/api/)
- 私聊文件通过 `notice_type=offline_file` 上报，`file` 为 `{name,size,url}`，可直接使用 URL。核对版本的官方 API 路由没有 `get_private_file_url`，不应无条件套用其他 OneBot 实现的此扩展。[事件文档](https://docs.go-cqhttp.org/event/)、[API 路由源码](https://github.com/Mrs4s/go-cqhttp/blob/a5923f179b360331786a6509eb33481e775a7bd1/modules/api/api.go)

## 建议验证场景

建议通过伪后端契约测试覆盖 string/array 上报、CQ 转义、嵌套原始合并记录、两种目标 API、作者/时间保留、媒体 URL 获取、失败响应与空节点，以及多个请求 echo 并发匹配。真实账号仍需验证后端登录、QQ 服务端风控、资源有效期与目标群/好友可达性；这些不能由本地契约测试证明。
