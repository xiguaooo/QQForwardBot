"""Resolve OneBot forward resources and build portable go-cqhttp custom nodes."""
from __future__ import annotations

from typing import Any

MAX_DEPTH = 5
MAX_NODES = 200


def _segments(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        value = [
            {"type": "node", "data": child}
            if isinstance(child, dict) and "type" not in child and ("content" in child or "sender" in child)
            else child
            for child in value
        ]
    # Deferred import keeps the client free to import this module at startup.
    from ws_client import message_to_segments
    return message_to_segments(value)


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": value}}


def _node(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    if value.get("type") == "node":
        value = value.get("data") or {}
    sender = value.get("sender") or {}
    return {
        "sender": {
            "user_id": sender.get("user_id") or value.get("uin") or value.get("user_id") or 0,
            "nickname": sender.get("nickname") or sender.get("card") or value.get("name") or value.get("nickname") or "未知用户",
        },
        "time": value.get("time") or "",
        "group_id": value.get("group_id") or 0,
        "content": _segments(value.get("content", value.get("message", []))),
    }


async def resolve_forward_segments(segments, request_action, *, group_id=0) -> list[dict[str, Any]]:
    """Expand nested resources with bounded depth/node count, retaining resource IDs."""
    count = 0
    cache: dict[str, Any] = {}

    async def walk(source, depth, ancestors):
        nonlocal count
        result = []
        for segment in _segments(source):
            kind = segment.get("type")
            data = dict(segment.get("data") or {})
            if kind not in {"forward", "node"}:
                result.append({"type": kind, "data": data})
                continue
            resource = str(data.get("id") or "")
            error = None
            if depth >= MAX_DEPTH:
                error = "达到嵌套深度限制"
            elif count >= MAX_NODES:
                error = "达到节点数量限制"
            elif resource and resource in ancestors:
                error = "循环引用"
            nodes = []
            if not error:
                if kind == "node":
                    if resource and "content" not in data and "message" not in data:
                        error = "引用节点内容不可用"
                    else:
                        nodes = [data]
                else:
                    nodes = data.get("messages", data.get("message"))
                    if nodes is None and resource:
                        try:
                            if resource not in cache:
                                response = await request_action("get_forward_msg", {"message_id": resource}, timeout=15)
                                if not isinstance(response, dict) or response.get("status") == "failed" or str(response.get("retcode", 0)) != "0":
                                    raise ValueError("get_forward_msg failed")
                                payload = response.get("data") or {}
                                cache[resource] = payload.get("messages", payload.get("message"))
                            nodes = cache[resource]
                        except Exception:
                            error = "读取失败"
                    if not error and not isinstance(nodes, list):
                        error = "内容不可用"
            resolved = []
            if not error:
                for original in nodes:
                    if count >= MAX_NODES:
                        resolved.append(_node({"content": [_text("[合并转发：达到节点数量限制]")]}))
                        break
                    count += 1
                    node = _node(original)
                    node["content"] = await walk(node["content"], depth + 1, ancestors | ({resource} if resource else set()))
                    resolved.append(node)
            else:
                data["error"] = error
                resolved = [_node({"content": [_text(f"[合并转发 {resource}：{error}]")]} )]
            if kind == "node":
                data.update(resolved[0] if resolved else _node({}))
            else:
                data["messages"] = resolved
            result.append({"type": kind, "data": data})
        return result

    return await walk(segments, 0, set())


def build_forward_nodes(items, *, self_id=0) -> list[dict[str, Any]]:
    """Convert rendered history into custom nodes without backend-local references."""
    count = 0

    def custom_node(node, depth):
        nonlocal count
        count += 1
        sender = node["sender"]
        try:
            uin = int(sender.get("user_id") or self_id or 10000)
        except (TypeError, ValueError):
            uin = int(self_id or 10000)
        data = {"name": str(sender.get("nickname") or uin), "uin": str(uin)}
        if node.get("time"):
            data["time"] = node["time"]
        data["content"] = content(node["content"], depth, data)
        return {"type": "node", "data": data}

    def content(source, depth, author):
        nonlocal count
        result = []
        for segment in _segments(source):
            kind = segment.get("type")
            data = dict(segment.get("data") or {})
            if kind in {"forward", "node"}:
                nodes = data.get("messages") if kind == "forward" else [data]
                if depth >= MAX_DEPTH or count >= MAX_NODES or not nodes or data.get("error"):
                    result.append(_text(f"[合并转发 {data.get('id', '')}：{data.get('error') or '内容不可用或达到展开限制'}]"))
                else:
                    for node in nodes:
                        if count >= MAX_NODES:
                            result.append(_text("[合并转发：达到节点数量限制]"))
                            break
                        result.append(custom_node(_node(node), depth + 1))
            elif kind in {"gray", "gray_tip", "notice", "poke"}:
                result.append(_text(str(data.get("text") or data.get("message") or ("戳一戳" if kind == "poke" else "系统消息"))))
            elif kind == "reply":
                result.append(_text(f"[回复消息 {data.get('id', '')}]"))
            elif kind == "file":
                name = str(data.get("name") or data.get("file_name") or "文件")
                url = str(data.get("url") or "")
                result.append(_text(f"[文件：{name}]" + (f" {url}" if url else "（下载链接不可用）")))
            else:
                # Strip renderer enrichment and receive-only metadata.
                allowed = {
                    "text": {"text"}, "at": {"qq"}, "face": {"id"},
                    "image": {"file", "type", "cache", "proxy", "timeout"},
                    "record": {"file", "magic", "cache", "proxy", "timeout"},
                    "video": {"file", "cover", "c"},
                    "json": {"data", "resid"}, "xml": {"data", "resid"},
                    "music": {"type", "id", "url", "audio", "title", "content", "image"},
                    "share": {"url", "title", "content", "image"},
                }.get(kind)
                if allowed is None:
                    result.append(_text(f"[{kind}]"))
                    continue
                if kind in {"image", "record", "video"} and data.get("url"):
                    data["file"] = data["url"]
                result.append({"type": kind, "data": {key: value for key, value in data.items() if key in allowed}})
        if any(segment["type"] == "node" for segment in result):
            # go-cqhttp interprets the entire content as nested forwarding once
            # it sees a node. Preserve surrounding ordinary segments in nodes
            # attributed to their original author, in their original order.
            normalized = []
            ordinary = []

            def flush_ordinary():
                nonlocal count
                if ordinary:
                    count += 1
                    normalized.append({"type": "node", "data": {**{key: value for key, value in author.items() if key in {"name", "uin", "time"}}, "content": ordinary.copy()}})
                    ordinary.clear()

            for segment in result:
                if segment["type"] == "node":
                    flush_ordinary()
                    normalized.append(segment)
                else:
                    ordinary.append(segment)
            flush_ordinary()
            if count > MAX_NODES:
                return [_text("[合并转发：达到节点数量限制]")]
            return normalized
        return result or [_text("[空消息]")]

    result = []
    for item in items:
        if count >= MAX_NODES:
            break
        render = item.get("render") or {}
        node = _node({"sender": {"user_id": render.get("sender_id") or self_id, "nickname": render.get("sender_nickname")}, "time": render.get("timestamp") or item.get("timestamp") or "", "content": item.get("segments", render.get("segments", []))})
        result.append(custom_node(node, 0))
    return result
