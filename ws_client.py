from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from websockets.legacy.client import WebSocketClientProtocol, connect

from config import ConfigManager, ForwardGroupConfig
from forward_messages import build_forward_nodes, resolve_forward_segments
from renderer import render_message_history_image, render_message_image

try:
    from qzone_sdk import ManualCookieProvider, QZoneClient, QZoneConfig
except ImportError:
    ManualCookieProvider = QZoneClient = QZoneConfig = None


LOGGER = logging.getLogger("qq_forwarder")
CQ_RE = re.compile(r"\[CQ:(?P<type>[^,\]]+)(?P<params>(?:,[^\]]*)?)\]")
MS_COMMAND_RE = re.compile(r"^\s*/ms(?:\s+(?P<count>\d+))?\s*$", re.IGNORECASE)
QZ_COMMAND_RE = re.compile(r"^\s*/qz(?:\s+(?P<count>\d+))?\s*$", re.IGNORECASE)
STATUS_COMMAND_RE = re.compile(r"^\s*/status\s*$", re.IGNORECASE)
HELP_COMMAND_RE = re.compile(r"^\s*/help\s*$", re.IGNORECASE)
QZ_ACTION_NAMES = (
    "get_emotion_list",
    "get_friend_feeds",
)
HELP_TEXT = "\n".join(
    [
        "QQ Forwarder 用法",
        "/ms <条数>  查询源群最近消息，按本组方式发送长图或合并转发",
        "/qz <条数>  查询监听 QQ 的动态，按本组方式发送",
        "/status     查看 WebSocket 与本分组状态",
        "/help       查看本帮助",
        "长图中语音、视频、文件显示占位并另行发送；原生转发中文件显示下载链接。",
    ]
)


class RuntimeState:
    def __init__(self) -> None:
        self.connected = False
        self.backend = "auto"
        self.self_id: int | None = None
        self.qq_online: bool | None = None
        self.connected_since: str | None = None
        self.last_error: str | None = None
        self.reconnect_count = 0
        self.pending_batch_count = 0
        self.qz_forward_count = 0
        self.logs: deque[dict[str, Any]] = deque(maxlen=100)
        self.forward_records: deque[dict[str, Any]] = deque(maxlen=100)

    def set_connected(self, connected: bool, *, error: str | None = None) -> None:
        self.connected = connected
        if connected:
            self.connected_since = datetime.now().isoformat(timespec="seconds")
            self.last_error = None
        else:
            self.connected_since = None
            if error:
                self.last_error = error

    def log(self, level: str, message: str) -> None:
        now = datetime.now()
        entry = {
            "time": now.strftime("%H:%M:%S"),
            "timestamp": now.isoformat(timespec="seconds"),
            "level": level.upper(),
            "message": message,
        }
        self.logs.append(entry)
        log_method = getattr(LOGGER, level.lower(), LOGGER.info)
        log_method(message)

    def record_forward(self, record: dict[str, Any]) -> None:
        self.forward_records.append(record)

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "backend": self.backend,
            "self_id": self.self_id,
            "qq_online": self.qq_online,
            "connected_since": self.connected_since,
            "last_error": self.last_error,
            "reconnect_count": self.reconnect_count,
            "pending_batch_count": self.pending_batch_count,
            "qz_forward_count": self.qz_forward_count,
            "log_count": len(self.logs),
            "forward_count": len(self.forward_records),
        }


class SnowLumaForwarder:
    def __init__(self, config_manager: ConfigManager, state: RuntimeState) -> None:
        self.config_manager = config_manager
        self.state = state
        self._stop_event = asyncio.Event()
        self._ws: WebSocketClientProtocol | None = None
        self._send_lock = asyncio.Lock()
        self._pending_actions: dict[str, dict[str, Any]] = {}
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._batch_buffers: dict[str, list[dict[str, Any]]] = {}
        self._batch_normal_counts: dict[str, int] = {}
        self._group_name_cache: dict[int, str] = {}
        self._group_info_cache: dict[int, dict[str, Any]] = {}
        self._member_cache: dict[tuple[int, str], dict[str, Any]] = {}
        self._avatar_cache: dict[str, Any] = {}
        self._avatar_attempted: set[str] = set()
        self._qz_seen: dict[tuple[str, str], deque[str]] = {}
        self._qz_initialized: set[tuple[str, str]] = set()
        self._qz_disabled_actions: set[str] = set()
        self._qz_warned_actions: set[str] = set()
        self._qz_sdk_client: Any = None
        self._qz_sdk_warned = False
        self._qz_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def run_forever(self) -> None:
        self.state.log("info", "OneBot 协议端 WebSocket 客户端任务已启动")
        while not self._stop_event.is_set():
            cfg = self.config_manager.get()
            try:
                self.state.log("info", "正在连接 OneBot WebSocket 后端")
                async with connect(
                    cfg.snowluma_ws_url,
                    extra_headers=_build_auth_headers() or None,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=16 * 1024 * 1024,
                ) as websocket:
                    self._ws = websocket
                    self._ready.clear()
                    self.state.backend = cfg.backend
                    self.state.set_connected(True)
                    self.state.log("info", "OneBot WebSocket 已连接")
                    self._qz_disabled_actions.clear()
                    self._qz_sdk_client = None
                    initialization = asyncio.create_task(self._initialize_backend(), name="onebot-init")
                    self._qz_task = asyncio.create_task(self._qz_poll_loop(), name="qz-poll")
                    try:
                        await self._receive_loop(websocket)
                    finally:
                        initialization.cancel()
                        await asyncio.gather(initialization, return_exceptions=True)
                        if self._qz_task is not None:
                            self._qz_task.cancel()
                            await asyncio.gather(self._qz_task, return_exceptions=True)
                            self._qz_task = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                self.state.set_connected(False, error=error_text)
                self.state.log("error", f"WebSocket 连接异常: {error_text}")
            finally:
                self._ws = None
                self._ready.clear()
                self.state.qq_online = None
                for task in list(self._message_tasks):
                    task.cancel()
                if self._message_tasks:
                    await asyncio.gather(*list(self._message_tasks), return_exceptions=True)
                self.state.set_connected(False)
                self._mark_pending_unknown()

            if self._stop_event.is_set():
                break
            self.state.reconnect_count += 1
            delay = random.uniform(3.0, 5.0)
            self.state.log("warning", f"将在 {delay:.1f} 秒后重连")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self.state.log("info", "OneBot 协议端 WebSocket 客户端任务已停止")

    async def _initialize_backend(self) -> None:
        try:
            version = await self._request_action("get_version_info", {}, timeout=5)
            data = version.get("data") if _response_ok(version) else None
            if self.state.backend == "auto" and isinstance(data, dict):
                name = str(data.get("app_name") or "").lower()
                if "go-cqhttp" in name:
                    self.state.backend = "go-cqhttp"
                elif "napcat" in name:
                    self.state.backend = "napcat"
            login = await self._request_action("get_login_info", {}, timeout=5)
            data = login.get("data") if _response_ok(login) else None
            if isinstance(data, dict):
                self.state.self_id = _safe_int(data.get("user_id"))
            self.state.log("info", f"OneBot 后端类型：{self.state.backend}")
        finally:
            self._ready.set()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            await self._ws.close(code=1000, reason="application shutdown")
        if self._qz_task is not None:
            self._qz_task.cancel()
            await asyncio.gather(self._qz_task, return_exceptions=True)
            self._qz_task = None
        for task in self._message_tasks:
            task.cancel()
        if self._message_tasks:
            await asyncio.gather(*self._message_tasks, return_exceptions=True)
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

    async def force_reconnect(self) -> None:
        if self._ws is not None:
            self.state.log("info", "配置已更新，正在重建 WebSocket 连接")
            await self._ws.close(code=1012, reason="configuration changed")

    async def _receive_loop(self, websocket: WebSocketClientProtocol) -> None:
        async for raw in websocket:
            packet = self._decode_packet(raw)
            if packet is None:
                continue
            echo = str(packet.get("echo", ""))
            if echo in self._pending_requests:
                future = self._pending_requests[echo]
                if not future.done():
                    future.set_result(packet)
                continue
            if echo in self._pending_actions:
                self._handle_action_response(packet)
                continue
            if packet.get("post_type") == "meta_event":
                self.state.self_id = _safe_int(packet.get("self_id")) or self.state.self_id
                status = packet.get("status")
                if isinstance(status, dict) and isinstance(status.get("online"), bool):
                    self.state.qq_online = status["online"]
                continue
            if _is_qz_event(packet):
                self._track_task(self._handle_qz_event(packet), "qz-event")
            elif packet.get("post_type") == "message" and packet.get("message_type") == "group":
                self._track_task(self._handle_group_message(packet), "group-message")
            elif packet.get("post_type") == "notice" and _safe_int(packet.get("group_id")):
                self._track_task(self._handle_group_notice(packet), "group-notice")

    def _track_task(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)
        task.add_done_callback(self._log_message_task_error)

    def _log_message_task_error(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.state.log("error", f"message handling failed: {type(error).__name__}: {error}")

    def _decode_packet(self, raw: str | bytes) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError:
            self.state.log("warning", "收到无法解析的非 JSON WebSocket 数据，已忽略")
            return None
        return packet if isinstance(packet, dict) else None

    async def _handle_group_message(self, event: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ready.wait()
        self.state.self_id = _safe_int(event.get("self_id")) or self.state.self_id
        group_id = _safe_int(event.get("group_id"))
        if not group_id:
            return
        cfg = self.config_manager.get()
        source_routes = [group for group in cfg.groups if group.source_group_id == group_id and group.target_group_ids]
        target_routes = [group for group in cfg.groups if group_id in group.target_group_ids]
        command = _command_text(event)
        if target_routes and command:
            handled = False
            for route in target_routes:
                handled = await self._handle_target_command(event, route) or handled
            if handled:
                return
        for route in source_routes:
            await self._handle_source_event(event, route)

    async def _handle_target_command(self, event: dict[str, Any], route: ForwardGroupConfig) -> bool:
        command = _command_text(event)
        target_group_id = _safe_int(event.get("group_id")) or 0
        if HELP_COMMAND_RE.fullmatch(command):
            await self._submit_group_message(
                target_group_id,
                HELP_TEXT,
                {"content_type": "help", "route_id": route.id, "target_group_id": target_group_id, "message": command},
            )
            return True
        if STATUS_COMMAND_RE.fullmatch(command):
            status_text = "\n".join(
                [
                    "QQ Forwarder status",
                    f"WebSocket: {'connected' if self.state.connected else 'disconnected'}",
                    f"分组: {route.name}",
                    f"源群: {route.source_group_id}",
                    f"目标群: {', '.join(str(value) for value in route.target_group_ids)}",
                    f"批量条数: {route.batch_size}",
                    f"待转发普通消息: {self._batch_normal_counts.get(route.id, 0)}",
                ]
            )
            await self._submit_group_message(
                target_group_id,
                status_text,
                {"content_type": "status", "route_id": route.id, "target_group_id": target_group_id, "message": command},
            )
            return True
        match = MS_COMMAND_RE.fullmatch(command)
        if match:
            await self._handle_ms_command(route, target_group_id, match.group("count"))
            return True
        match = QZ_COMMAND_RE.fullmatch(command)
        if match:
            await self._handle_qz_command(route, target_group_id, match.group("count"))
            return True
        return False

    async def _handle_ms_command(self, route: ForwardGroupConfig, target_group_id: int, count_text: str | None) -> None:
        if not count_text:
            await self._submit_group_message(target_group_id, "用法：/ms <条数>，范围为 1~50。", {"content_type": "ms_error", "route_id": route.id, "message": "/ms"})
            return
        count = int(count_text)
        if not 1 <= count <= 50:
            await self._submit_group_message(target_group_id, "用法：/ms <条数>，范围为 1~50。", {"content_type": "ms_error", "route_id": route.id, "message": count_text})
            return
        history = await self._fetch_group_history(route.source_group_id, count)
        if history is None:
            await self._submit_group_message(target_group_id, "源群历史消息暂不可用。", {"content_type": "ms_error", "route_id": route.id, "message": count_text})
            return
        if not history:
            await self._submit_group_message(target_group_id, "源群没有可用消息。", {"content_type": "ms_error", "route_id": route.id, "message": count_text})
            return
        items = await self._history_to_items(route, history)
        await self._send_batch(route, items, content_type="ms_image", command_label=f"/ms {count}")

    async def _handle_qz_command(self, route: ForwardGroupConfig, target_group_id: int, count_text: str | None) -> None:
        if not count_text:
            await self._submit_group_message(target_group_id, "用法：/qz <条数>，范围为 1~50。", {"content_type": "qz_error", "route_id": route.id, "message": "/qz"})
            return
        count = int(count_text)
        if not 1 <= count <= 50:
            await self._submit_group_message(target_group_id, "用法：/qz <条数>，范围为 1~50。", {"content_type": "qz_error", "route_id": route.id, "message": count_text})
            return
        if not route.watched_qq_ids:
            await self._submit_group_message(target_group_id, "本分组没有配置要监听的 QQ 号。", {"content_type": "qz_error", "route_id": route.id, "message": count_text})
            return
        dynamics: list[dict[str, Any]] = []
        for user_id in route.watched_qq_ids:
            fetched = await self._fetch_qz_dynamics(user_id, count, route.qz_action)
            if fetched:
                dynamics.extend(fetched)
        dynamics = _unique_dynamics(dynamics)
        dynamics.sort(key=lambda item: (item.get("timestamp") or 0, str(item.get("message_id") or "")))
        dynamics = dynamics[-count:]
        if not dynamics:
            await self._submit_group_message(target_group_id, "QQ 动态暂不可用或没有找到动态。", {"content_type": "qz_error", "route_id": route.id, "message": count_text})
            return
        for item in dynamics:
            self._remember_qz_item(route, item)
        items = [await self._dynamic_to_item(route, item) for item in dynamics]
        await self._send_batch(route, items, content_type="qz_image", command_label=f"/qz {count}")

    async def _handle_source_event(
        self,
        event: dict[str, Any],
        route: ForwardGroupConfig,
        special_segments: list[dict[str, Any]] | None = None,
    ) -> None:
        lock = self._route_locks.setdefault(route.id, asyncio.Lock())
        flush: list[dict[str, Any]] = []
        async with lock:
            segments = special_segments or message_to_segments(event.get("message"), event.get("raw_message"))
            is_gray = _is_gray_event(event, segments)
            if is_gray and special_segments is None:
                segments = _gray_segments(event, segments)
            item = await self._event_to_item(route, event, segments, is_gray=is_gray)
            if item is None:
                return
            buffer = self._batch_buffers.setdefault(route.id, [])
            buffer.append(item)
            normal_count = self._batch_normal_counts.get(route.id, 0)
            if not item["is_gray"]:
                normal_count += 1
                if normal_count >= route.batch_size:
                    flush = list(buffer)
                    buffer.clear()
                    normal_count = 0
            self._batch_normal_counts[route.id] = normal_count
            self.state.pending_batch_count = sum(len(values) for values in self._batch_buffers.values())
            if flush:
                await self._send_batch(route, flush, content_type="rendered_image", command_label=None)

    async def _handle_group_notice(self, event: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ready.wait()
        group_id = _safe_int(event.get("group_id"))
        if not group_id:
            return
        cfg = self.config_manager.get()
        routes = [group for group in cfg.groups if group.source_group_id == group_id and group.target_group_ids]
        for route in routes:
            segments = await self._notice_segments(route, event)
            await self._handle_source_event(event, route, segments)

    async def _event_to_item(
        self,
        route: ForwardGroupConfig,
        event: dict[str, Any],
        segments: list[dict[str, Any]],
        *,
        is_gray: bool,
    ) -> dict[str, Any] | None:
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = str(event.get("user_id") or event.get("operator_id") or sender.get("user_id") or "")
        member = await self._fetch_group_member_info(route.source_group_id, sender_id) if sender_id else {}
        sender_nickname = str(sender.get("card") or sender.get("nickname") or member.get("card") or member.get("nickname") or sender_id or "系统消息")
        role = str(sender.get("role") or member.get("role") or "member")
        member_title = str(sender.get("title") or member.get("title") or "")
        segments = await self._resolve_at_segments(route.source_group_id, segments)
        segments = await resolve_forward_segments(segments, self._request_action, group_id=route.source_group_id)
        segments = await self._resolve_media_segments(segments)
        reply_id = _reply_message_id(segments)
        quote = await self._fetch_quoted_message(reply_id) if reply_id is not None and not is_gray else None
        avatar = await self._resolve_avatar(sender_id, sender, member)
        message_text = message_to_readable_text(segments)
        event_time = _event_hhmm(event.get("time"))
        formatted = _format_template(route, sender_nickname, sender_id, event_time, message_text)
        return {
            "render": {
                "sender_nickname": sender_nickname,
                "sender_id": sender_id,
                "event_time": event_time,
                "timestamp": _safe_timestamp(event.get("time")),
                "segments": segments,
                "member_title": member_title,
                "role": role,
                "quote": quote,
                "outgoing": str(sender_id) == str(event.get("self_id") or ""),
                "is_gray": is_gray,
                "avatar": avatar,
            },
            "segments": segments,
            "message_text": message_text,
            "formatted_message": formatted,
            "message_id": event.get("message_id"),
            "is_gray": is_gray,
            "counts_toward_batch": not is_gray,
        }

    async def _history_to_items(self, route: ForwardGroupConfig, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for original in history:
            segments = original.get("segments") if isinstance(original.get("segments"), list) else []
            segments = await resolve_forward_segments(segments, self._request_action, group_id=route.source_group_id)
            segments = await self._resolve_media_segments(segments)
            quote = None
            reply_id = _reply_message_id(segments)
            if reply_id is not None:
                quote = await self._fetch_quoted_message(reply_id)
            sender_id = str(original.get("sender_id") or "")
            member = await self._fetch_group_member_info(route.source_group_id, sender_id) if sender_id else {}
            nickname = str(original.get("sender_nickname") or member.get("card") or member.get("nickname") or sender_id or "Unknown")
            role = str(original.get("role") or member.get("role") or "member")
            title = str(original.get("member_title") or member.get("title") or "")
            avatar = await self._resolve_avatar(sender_id, original.get("avatar"), member)
            is_gray = bool(original.get("is_gray"))
            items.append(
                {
                    "render": {
                        "sender_nickname": nickname,
                        "sender_id": sender_id,
                        "event_time": str(original.get("time") or ""),
                        "timestamp": original.get("timestamp"),
                        "segments": segments,
                        "member_title": title,
                        "role": role,
                        "quote": quote,
                        "outgoing": bool(original.get("outgoing")),
                        "is_gray": is_gray,
                        "avatar": avatar,
                    },
                    "segments": segments,
                    "message_text": message_to_readable_text(segments),
                    "formatted_message": message_to_readable_text(segments),
                    "message_id": original.get("message_id"),
                    "is_gray": is_gray,
                    "counts_toward_batch": not is_gray,
                }
            )
        return items

    async def _dynamic_to_item(self, route: ForwardGroupConfig, dynamic: dict[str, Any]) -> dict[str, Any]:
        sender_id = str(dynamic.get("sender_id") or dynamic.get("qz_user_id") or "")
        member = await self._fetch_group_member_info(route.source_group_id, sender_id) if sender_id else {}
        nickname = str(dynamic.get("sender_nickname") or member.get("card") or member.get("nickname") or sender_id or "QQ 动态")
        segments = dynamic.get("segments") if isinstance(dynamic.get("segments"), list) else []
        segments = await self._resolve_at_segments(route.source_group_id, segments)
        segments = await self._resolve_media_segments(segments)
        reply_id = _reply_message_id(segments)
        quote = await self._fetch_quoted_message(reply_id) if reply_id is not None else None
        avatar = await self._resolve_avatar(sender_id, dynamic.get("avatar"), member)
        text = message_to_readable_text(segments)
        event_time = str(dynamic.get("time") or "")
        return {
            "render": {
                "sender_nickname": nickname,
                "sender_id": sender_id,
                "event_time": event_time,
                "segments": segments,
                "member_title": str(member.get("title") or ""),
                "role": str(member.get("role") or "member"),
                "quote": quote,
                "outgoing": False,
                "avatar": avatar,
            },
            "segments": segments,
            "message_text": text,
            "formatted_message": _format_template(route, nickname, sender_id, event_time, text),
            "message_id": dynamic.get("message_id"),
            "is_gray": False,
            "counts_toward_batch": False,
        }

    async def _send_batch(
        self,
        route: ForwardGroupConfig,
        items: list[dict[str, Any]],
        *,
        content_type: str,
        command_label: str | None,
    ) -> None:
        if not items or not route.target_group_ids:
            return
        if route.forward_mode == "forward":
            nodes = build_forward_nodes(items, self_id=self.state.self_id or 0)
            for target_group_id in route.target_group_ids:
                await self._submit_group_forward(target_group_id, nodes, {
                    "route_id": route.id, "route_name": route.name,
                    "source_group_id": route.source_group_id, "target_group_id": target_group_id,
                    "message_ids": [item.get("message_id") for item in items],
                    "message": command_label or "\n".join(str(item.get("message_text") or "") for item in items),
                    "content_type": "merged_forward",
                })
                for item in items:
                    for segment in _separate_media_segments(item.get("segments", [])):
                        if segment.get("type") == "file":
                            await self._submit_media(target_group_id, segment, {
                                "route_id": route.id, "route_name": route.name,
                                "source_group_id": route.source_group_id, "target_group_id": target_group_id,
                                "message": item.get("message_text", "文件"),
                            })
            if content_type.startswith("qz"):
                self.state.qz_forward_count += len(items)
            return
        render_messages = [item["render"] for item in items]
        message_ids = [item.get("message_id") for item in items]
        joined_text = "\n".join(str(item.get("message_text") or "[空消息]") for item in items)
        image_bytes: bytes | None = None
        try:
            group_info = await self._fetch_group_info(route.source_group_id)
            image_bytes = await asyncio.to_thread(
                render_message_history_image,
                group_name=str(group_info.get("group_name") or f"QQ 群 {route.source_group_id}"),
                group_member_count=_safe_int(group_info.get("member_count")) or _safe_int(group_info.get("member_num")),
                messages=render_messages,
            )
        except Exception as exc:
            self.state.log("error", f"{content_type} 图片渲染失败: {exc}")

        for target_group_id in route.target_group_ids:
            pending = {
                "route_id": route.id,
                "route_name": route.name,
                "source_group_id": route.source_group_id,
                "target_group_id": target_group_id,
                "message_ids": message_ids,
                "message": command_label or joined_text,
                "formatted_message": joined_text,
                "content_type": content_type if image_bytes is not None else f"{content_type}_text_fallback",
            }
            if image_bytes is None:
                await self._submit_group_message(target_group_id, joined_text, pending)
            else:
                image_message = [{"type": "image", "data": {"file": "base64://" + base64.b64encode(image_bytes).decode("ascii")}}]
                await self._submit_group_message(target_group_id, image_message, pending)
            for item in items:
                for media_segment in _separate_media_segments(item.get("segments", [])):
                    await self._submit_media(target_group_id, media_segment, pending)
        if content_type.startswith("qz"):
            self.state.qz_forward_count += len(items)

    async def _notice_segments(self, route: ForwardGroupConfig, event: dict[str, Any]) -> list[dict[str, Any]]:
        notice_type = str(event.get("notice_type") or "通知")
        if notice_type == "group_upload" and isinstance(event.get("file"), dict):
            data = dict(event["file"])
            data["file_id"] = data.get("id")
            data["group_id"] = route.source_group_id
            response = await self._request_action("get_group_file_url", {
                "group_id": route.source_group_id, "file_id": data.get("id"),
                "busid": data.get("busid", 0),
            }, timeout=8)
            if _response_ok(response) and isinstance(response.get("data"), dict):
                data["url"] = response["data"].get("url")
            return [{"type": "file", "data": data}]
        sub_type = str(event.get("sub_type") or "")
        operator_id = str(event.get("user_id") or event.get("operator_id") or "")
        if sub_type.lower() == "poke" or notice_type.lower() == "notify" and event.get("target_id"):
            target_id = str(event.get("target_id") or "")
            operator = await self._fetch_group_member_info(route.source_group_id, operator_id) if operator_id else {}
            target = await self._fetch_group_member_info(route.source_group_id, target_id) if target_id else {}
            operator_name = str(operator.get("card") or operator.get("nickname") or operator_id or "某人")
            target_name = str(target.get("card") or target.get("nickname") or target_id or "某人")
            return [{"type": "poke", "data": {"text": f"{operator_name} 戳了戳 {target_name}"}}]
        target_id = str(event.get("target_id") or "")
        text = f"群通知：{notice_type}{('/' + sub_type) if sub_type else ''}"
        if target_id:
            text += f"（QQ {target_id}）"
        return [{"type": "gray", "data": {"text": text}}]

    async def _fetch_group_history(self, group_id: int, count: int) -> list[dict[str, Any]] | None:
        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: int | None = None
        # go-cqhttp ignores count and returns up to 20 entries per request.
        for _ in range(5):
            params: dict[str, Any] = {"group_id": group_id}
            if self.state.backend != "go-cqhttp":
                params["count"] = count
            if cursor is not None:
                params["message_seq"] = cursor
            response = await self._request_action("get_group_msg_history", params, timeout=10)
            if not _response_ok(response):
                return None
            data = response.get("data")
            page = data.get("messages") if isinstance(data, dict) else data
            if not isinstance(page, list):
                return None
            added = 0
            sequences: list[int] = []
            for message in page:
                if not isinstance(message, dict):
                    continue
                seq = _safe_int(message.get("message_seq"))
                if seq is not None:
                    sequences.append(seq)
                identity = str(message.get("message_id", message.get("message_seq", json.dumps(message, sort_keys=True))))
                if identity in seen:
                    continue
                seen.add(identity)
                messages.append(message)
                added += 1
            if len(messages) >= count or not added or not sequences:
                break
            next_cursor = min(sequences) - 1
            if next_cursor <= 0 or cursor is not None and next_cursor >= cursor:
                break
            cursor = next_cursor
        result: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
            sender_id = str(message.get("user_id") or sender.get("user_id") or "")
            segments = message_to_segments(message.get("message"), message.get("raw_message"))
            segments = await self._resolve_at_segments(group_id, segments)
            is_gray = _is_gray_event(message, segments)
            if is_gray:
                segments = _gray_segments(message, segments)
            result.append(
                {
                    "group_id": _safe_int(message.get("group_id")),
                    "message_id": message.get("message_id"),
                    "message_seq": _safe_int(message.get("message_seq")),
                    "timestamp": _safe_timestamp(message.get("time")),
                    "time": _event_hhmm(message.get("time")),
                    "sender_id": sender_id,
                    "sender_nickname": str(sender.get("card") or sender.get("nickname") or sender_id or "系统消息"),
                    "role": str(sender.get("role") or ""),
                    "member_title": str(sender.get("title") or ""),
                    "text": message_to_readable_text(segments),
                    "segments": segments,
                    "is_gray": is_gray,
                    "outgoing": str(sender_id) == str(message.get("self_id") or ""),
                }
            )
        result.sort(key=lambda item: (item.get("timestamp") or 0, item.get("message_seq") or 0))
        return result[-count:]

    async def _fetch_qz_dynamics(
        self,
        user_id: str,
        count: int,
        action_name: str | None = None,
    ) -> list[dict[str, Any]] | None:
        action_name = (action_name or QZ_ACTION_NAMES[0]).strip()
        if action_name in {"get_emotion_list", "qzone_sdk.get_friend_moods"}:
            sdk_items = await self._fetch_qz_with_sdk(user_id, count)
            if sdk_items is not None:
                return _normalize_qz_data(sdk_items, user_id)
        if action_name in self._qz_disabled_actions:
            return None
        if action_name not in QZ_ACTION_NAMES:
            self.state.log("warning", f"Qzone 动作 {action_name} 不在已知适配列表中，将按自定义动作尝试")
        params = {
            "user_id": int(user_id) if user_id.isdigit() else user_id,
            "count": count,
            "num": count,
            "pos": 0,
            "include_image_data": True,
        }
        response = await self._request_action(action_name, params, timeout=12)
        if _response_ok(response):
            return _normalize_qz_data(response.get("data"), user_id)
        if _is_unsupported_response(response):
            self._qz_disabled_actions.add(action_name)
            if action_name not in self._qz_warned_actions:
                self.state.log(
                    "warning",
                    f"OneBot 后端不支持 {action_name}；/qz 已暂停重试，请安装 onebot-qzone bridge 并在 WebUI 选择对应动作",
                )
                self._qz_warned_actions.add(action_name)
        return None

    async def _fetch_qz_with_sdk(self, user_id: str, count: int) -> list[dict[str, Any]] | None:
        if QZoneClient is None or QZoneConfig is None or ManualCookieProvider is None:
            return None
        try:
            if self._qz_sdk_client is None:
                cookie_response = await self._request_action(
                    "get_cookies",
                    {"domain": "user.qzone.qq.com"},
                    timeout=8,
                )
                login_response = await self._request_action("get_login_info", {}, timeout=5)
                cookie_data = cookie_response.get("data") if _response_ok(cookie_response) else {}
                login_data = login_response.get("data") if _response_ok(login_response) else {}
                cookies = str(cookie_data.get("cookies") or "") if isinstance(cookie_data, dict) else ""
                login_id = str(login_data.get("user_id") or "") if isinstance(login_data, dict) else ""
                if not cookies or not login_id:
                    return None
                provider = ManualCookieProvider(cookies, login_id)
                self._qz_sdk_client = QZoneClient(QZoneConfig(auth_provider=provider, timeout=20))
            return await asyncio.to_thread(_run_qzone_sdk_moods, self._qz_sdk_client, user_id, count)
        except Exception as exc:
            if not self._qz_sdk_warned:
                self.state.log("warning", f"qzone-sdk 查询失败，将尝试 OneBot Qzone 动作: {type(exc).__name__}: {exc}")
                self._qz_sdk_warned = True
            self._qz_sdk_client = None
            return None

    async def _qz_poll_loop(self) -> None:
        await self._ready.wait()
        last_poll: dict[tuple[str, str], float] = {}
        while not self._stop_event.is_set():
            cfg = self.config_manager.get()
            now = time.monotonic()
            for route in cfg.groups:
                for user_id in route.watched_qq_ids:
                    key = (route.id, user_id)
                    if now - last_poll.get(key, 0) < route.qz_poll_interval:
                        continue
                    last_poll[key] = now
                    dynamics = await self._fetch_qz_dynamics(user_id, 20, route.qz_action)
                    if dynamics is None:
                        continue
                    seen = self._qz_seen.setdefault(key, deque(maxlen=500))
                    if key not in self._qz_initialized:
                        for item in dynamics:
                            identity = _dynamic_identity(item)
                            if identity not in seen:
                                seen.append(identity)
                        self._qz_initialized.add(key)
                        continue
                    for item in dynamics:
                        if not self._remember_qz_item(route, item):
                            continue
                        render_item = await self._dynamic_to_item(route, item)
                        await self._send_batch(route, [render_item], content_type="qz_image", command_label=None)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _handle_qz_event(self, packet: dict[str, Any]) -> None:
        payload = packet.get("data") if isinstance(packet.get("data"), (dict, list)) else packet
        default_user = _first_value(payload, "user_id", "qq", "uin") if isinstance(payload, dict) else ""
        dynamics = _normalize_qz_data(payload, str(default_user or ""))
        if not dynamics:
            return
        cfg = self.config_manager.get()
        for route in cfg.groups:
            watched = set(route.watched_qq_ids)
            for item in dynamics:
                user_id = str(item.get("sender_id") or item.get("qz_user_id") or "")
                if user_id and user_id not in watched:
                    continue
                if not self._remember_qz_item(route, item):
                    continue
                render_item = await self._dynamic_to_item(route, item)
                await self._send_batch(route, [render_item], content_type="qz_image", command_label=None)

    def _remember_qz_item(self, route: ForwardGroupConfig, item: dict[str, Any]) -> bool:
        user_id = str(item.get("sender_id") or item.get("qz_user_id") or "")
        key = (route.id, user_id)
        seen = self._qz_seen.setdefault(key, deque(maxlen=500))
        identity = _dynamic_identity(item)
        if identity in seen:
            return False
        seen.append(identity)
        return True

    async def _resolve_at_segments(self, group_id: int, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            data = dict(data)
            if str(segment.get("type", "")) == "at":
                qq = str(data.get("qq") or "").strip()
                if qq == "all":
                    data["display_name"] = "全体成员"
                else:
                    display_name = _at_display_name(data)
                    if not display_name and group_id > 0 and qq:
                        member = await self._fetch_group_member_info(group_id, qq)
                        display_name = str(member.get("card") or member.get("nickname") or "").strip()
                    if display_name:
                        data["display_name"] = display_name
            resolved.append({"type": str(segment.get("type", "unknown")), "data": data})
        return resolved

    async def _resolve_media_segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "unknown").lower()
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            data = dict(data)
            if segment_type in {"forward", "node"} and isinstance(data.get("messages"), list):
                data["messages"] = [dict(node) for node in data["messages"]]
                for node in data["messages"]:
                    content = message_to_segments(node.get("content"))
                    content = await self._resolve_at_segments(_safe_int(node.get("group_id")) or 0, content)
                    node["content"] = await self._resolve_media_segments(content)
            elif segment_type == "node" and isinstance(data.get("content"), list):
                content = await self._resolve_at_segments(_safe_int(data.get("group_id")) or 0, data["content"])
                data["content"] = await self._resolve_media_segments(content)
            if segment_type == "image" and not data.get("base64"):
                image_ref = next(
                    (
                        data.get(key)
                        for key in ("file", "file_id", "path", "url", "temp_url")
                        if data.get(key) not in (None, "")
                    ),
                    None,
                )
                if image_ref and not str(data.get("url") or image_ref).startswith(("https://", "http://", "base64://", "data:")):
                    response = await self._request_action(
                        "get_image",
                        {"file": str(image_ref)},
                        timeout=8,
                    )
                    image_data = response.get("data") if _response_ok(response) else None
                    if isinstance(image_data, dict):
                        for key in ("base64", "url", "file", "path"):
                            value = image_data.get(key)
                            if value not in (None, ""):
                                data[key] = value
            resolved.append({"type": str(segment.get("type", "unknown")), "data": data})
        return resolved

    async def _resolve_avatar(
        self,
        user_id: str,
        *sources: Any,
    ) -> Any:
        for source in sources:
            if isinstance(source, dict):
                nested = source.get("data") if isinstance(source.get("data"), dict) else {}
                for source_data in (source, nested):
                    for key in ("avatar", "avatar_url", "avatar_path", "url", "file", "path", "base64"):
                        value = source_data.get(key)
                        if value not in (None, ""):
                            return value
            elif source not in (None, ""):
                return source
        if not user_id:
            return None
        if self.state.backend == "go-cqhttp":
            # No official get_avatar action; the renderer uses the QQ avatar URL.
            return None
        if user_id in self._avatar_cache:
            return self._avatar_cache[user_id]
        if user_id in self._avatar_attempted:
            return None
        self._avatar_attempted.add(user_id)
        value: Any = int(user_id) if user_id.isdigit() else user_id
        response = await self._request_action("get_avatar", {"user_id": value}, timeout=6)
        avatar_data = response.get("data") if _response_ok(response) else None
        if isinstance(avatar_data, dict):
            for key in ("base64", "url", "file", "path"):
                candidate = avatar_data.get(key)
                if candidate not in (None, ""):
                    self._avatar_cache[user_id] = candidate
                    return candidate
        return None

    async def _fetch_group_member_info(self, group_id: int, user_id: str) -> dict[str, Any]:
        if not user_id:
            return {}
        cache_key = (group_id, user_id)
        if cache_key in self._member_cache:
            return self._member_cache[cache_key]
        response = await self._request_action(
            "get_group_member_info",
            {"group_id": group_id, "user_id": user_id, "no_cache": False},
            timeout=5,
        )
        data = response.get("data") if _response_ok(response) and isinstance(response.get("data"), dict) else {}
        if data:
            self._member_cache[cache_key] = data
        return data

    async def _fetch_quoted_message(self, message_id: Any) -> dict[str, Any] | None:
        response = await self._request_action("get_msg", {"message_id": message_id}, timeout=8)
        if not _response_ok(response) or not isinstance(response.get("data"), dict):
            return None
        data = response["data"]
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        sender_id = str(data.get("user_id") or sender.get("user_id") or "")
        segments = message_to_segments(data.get("message"), data.get("raw_message"))
        quoted_group_id = _safe_int(data.get("group_id")) or 0
        segments = await self._resolve_at_segments(quoted_group_id, segments)
        segments = await resolve_forward_segments(segments, self._request_action, group_id=quoted_group_id)
        segments = await self._resolve_media_segments(segments)
        return {
            "group_id": quoted_group_id,
            "sender_id": sender_id,
            "sender_nickname": str(sender.get("card") or sender.get("nickname") or sender_id or "Unknown"),
            "time": _event_hhmm(data.get("time")),
            "text": message_to_readable_text(segments),
            "segments": segments,
        }

    async def _fetch_group_info(self, group_id: int) -> dict[str, Any]:
        if group_id in self._group_info_cache:
            return self._group_info_cache[group_id]
        response = await self._request_action("get_group_info", {"group_id": group_id, "no_cache": False}, timeout=5)
        data = response.get("data") if _response_ok(response) and isinstance(response.get("data"), dict) else {}
        if data:
            self._group_info_cache[group_id] = data
        return data

    async def _fetch_group_name(self, group_id: int) -> str:
        data = await self._fetch_group_info(group_id)
        return str(data.get("group_name") or f"QQ 群 {group_id}")

    async def _request_action(self, action: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
        echo = f"qq-forwarder-request-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[echo] = future
        try:
            await self._send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            return None
        finally:
            self._pending_requests.pop(echo, None)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("WebSocket 当前未连接")
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await self._ws.send(text)

    async def _submit_group_message(self, target_group_id: int, message: str | list[dict[str, Any]], pending: dict[str, Any]) -> None:
        await self._submit_action("send_group_msg", {
                "group_id": target_group_id,
                "message": message,
                **({"auto_escape": True} if isinstance(message, str) else {}),
            }, pending)

    async def _submit_group_forward(self, target_group_id: int, nodes: list[dict[str, Any]], pending: dict[str, Any]) -> None:
        await self._submit_action("send_group_forward_msg", {
            "group_id": target_group_id, "messages": nodes,
        }, pending, timeout=60)

    async def _submit_media(self, target_group_id: int, segment: dict[str, Any], pending: dict[str, Any]) -> None:
        kind = str(segment.get("type"))
        data = dict(segment.get("data") or {})
        pending = {**pending, "content_type": kind}
        if kind == "file":
            source = data.get("url") or data.get("file") or data.get("path")
            if not source:
                self.state.record_forward({**pending, "status": "failed", "detail": "文件下载地址不可用"})
                return
            if str(source).startswith(("http://", "https://")):
                downloaded = await self._request_action("download_file", {
                    "url": source, "thread_count": 1,
                }, timeout=60)
                local_data = downloaded.get("data") if _response_ok(downloaded) else None
                source = local_data.get("file") if isinstance(local_data, dict) else None
                if not source:
                    self.state.record_forward({**pending, "status": "failed", "detail": "后端下载群文件失败"})
                    return
            await self._submit_action("upload_group_file", {
                "group_id": target_group_id, "file": source,
                "name": data.get("name") or data.get("file_name") or "附件",
            }, pending, timeout=60)
            return
        if data.get("url"):
            data["file"] = data["url"]
        await self._submit_group_message(target_group_id, [{"type": kind, "data": data}], pending)

    async def _submit_action(self, action: str, params: dict[str, Any], pending: dict[str, Any], *, timeout: float = 30) -> None:
        echo = f"qq-forwarder-{uuid.uuid4().hex}"
        pending = {**pending, "time": datetime.now().strftime("%H:%M:%S"), "action": action}
        self._pending_actions[echo] = pending
        response = await self._request_action(action, params, timeout=timeout)
        if response is None:
            if self._pending_actions.pop(echo, None) is not None:
                self.state.record_forward({**pending, "status": "unknown", "detail": "未收到动作响应；为避免重复消息未自动重发"})
                self.state.log("warning", f"{action} 未收到动作响应")
            return
        self._handle_action_response({**response, "echo": echo})

    def _handle_action_response(self, packet: dict[str, Any]) -> None:
        echo = str(packet.get("echo"))
        pending = self._pending_actions.pop(echo, None)
        if pending is None:
            return
        ok = _response_ok(packet)
        data = packet.get("data") if isinstance(packet.get("data"), dict) else {}
        record = {
            **pending,
            "status": "success" if ok else "failed",
            "detail": packet.get("wording") or packet.get("message") or "",
            "message_id": data.get("message_id"),
            "forward_id": data.get("forward_id"),
        }
        self.state.record_forward(record)
        if ok:
            self.state.log("info", f"发送成功，message_id={record['message_id']}")
        else:
            self.state.log("error", f"OneBot 返回发送失败: status={packet.get('status')}, retcode={packet.get('retcode')}, detail={record['detail']}")

    def _mark_pending_unknown(self) -> None:
        for pending in self._pending_actions.values():
            self.state.record_forward({**pending, "status": "unknown", "detail": "连接在动作响应返回前断开"})
        self._pending_actions.clear()
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(ConnectionError("WebSocket connection closed"))
        self._pending_requests.clear()


def message_to_segments(message: Any, raw_message: Any = None) -> list[dict[str, Any]]:
    if isinstance(message, list):
        segments: list[dict[str, Any]] = []
        for segment in message:
            if isinstance(segment, dict):
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                segments.append({"type": str(segment.get("type", "unknown")), "data": data})
            else:
                segments.append({"type": "text", "data": {"text": str(segment)}})
        return segments
    source = message if isinstance(message, str) else raw_message if isinstance(raw_message, str) else ""
    if not source:
        return []
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_RE.finditer(source):
        if match.start() > cursor:
            segments.append({"type": "text", "data": {"text": _unescape_cq(source[cursor:match.start()])}})
        segments.append({"type": match.group("type"), "data": _parse_cq_params(match.group("params"))})
        cursor = match.end()
    if cursor < len(source):
        segments.append({"type": "text", "data": {"text": _unescape_cq(source[cursor:])}})
    return segments or [{"type": "text", "data": {"text": _unescape_cq(source)}}]


def _run_qzone_sdk_moods(client: Any, user_id: str, count: int) -> list[dict[str, Any]]:
    async def fetch() -> list[dict[str, Any]]:
        result = await client.get_friend_moods(user_id, num=count)
        if isinstance(result, list) and result:
            return result

        feeds = await client.get_feed_list(count=max(count, 20))
        if not isinstance(feeds, list):
            return []
        target_id = str(user_id)
        return [
            item
            for item in feeds
            if str(item.get("uin") or item.get("opuin") or "") == target_id
        ][:count]

    return asyncio.run(fetch())


def _reply_message_id(segments: list[dict[str, Any]]) -> Any:
    for segment in segments:
        if segment.get("type") == "reply":
            data = segment.get("data")
            if isinstance(data, dict) and data.get("id") not in (None, ""):
                return data["id"]
    return None


def _separate_media_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for segment in segments:
        if segment.get("type") in {"record", "video", "file"}:
            result.append(segment)
        elif segment.get("type") in {"forward", "node"}:
            data = segment.get("data", {})
            nodes = data.get("messages", []) if segment.get("type") == "forward" else [data]
            for node in nodes:
                result.extend(_separate_media_segments(node.get("content", [])))
    return result


def _command_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                return ""
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            parts.append(str(data.get("text", "")))
        return "".join(parts).strip()
    if isinstance(message, str):
        return message.strip()
    raw_message = event.get("raw_message")
    return raw_message.strip() if isinstance(raw_message, str) else ""


def message_to_readable_text(message: Any, raw_message: Any = None) -> str:
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                parts.append(str(segment))
                continue
            seg_type = str(segment.get("type", "unknown"))
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            parts.append(_segment_to_text(seg_type, data))
        return "".join(parts).strip() or "[空消息]"
    if isinstance(message, str):
        return cq_to_readable_text(message).strip() or "[空消息]"
    if isinstance(raw_message, str):
        return cq_to_readable_text(raw_message).strip() or "[空消息]"
    if message is None:
        return "[空消息]"
    return str(message)


def _segment_to_text(seg_type: str, data: dict[str, Any]) -> str:
    if seg_type == "text":
        return str(data.get("text", ""))
    if seg_type == "at":
        qq = data.get("qq", "")
        return "@全体成员" if str(qq) == "all" else f"@{_at_display_name(data) or qq}"
    if seg_type == "reply":
        return f"[回复:{data.get('id', '')}]"
    if seg_type == "face":
        return f"[表情:{data.get('id', '')}]"
    if seg_type == "poke":
        return str(data.get("text") or "[戳一戳]")
    if seg_type in {"gray", "gray_tip", "notice"}:
        return str(data.get("text") or data.get("message") or "[系统消息]")
    if seg_type in {"image", "record", "video", "file"}:
        label = {"image": "图片", "record": "语音", "video": "视频", "file": "文件"}[seg_type]
        descriptor = _media_descriptor(data)
        return f"[{label}{(': ' + descriptor) if descriptor else ''}]"
    if seg_type == "json":
        return "[JSON卡片]"
    if seg_type == "xml":
        return "[XML卡片]"
    if seg_type == "forward":
        if data.get("error"):
            return f"[合并转发不可用: {data['error']}]"
        if isinstance(data.get("messages"), list):
            return "[合并转发]\n" + "\n".join(
                f"{node.get('sender', {}).get('nickname') or '未知发送者'}：{message_to_readable_text(node.get('content'))}"
                for node in data["messages"]
            )
        return f"[合并转发:{data.get('id', '')}]"
    if seg_type == "node":
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        return f"{sender.get('nickname') or data.get('name') or '未知发送者'}：{message_to_readable_text(data.get('content'))}"
    descriptor = _media_descriptor(data)
    return f"[{seg_type}{(': ' + descriptor) if descriptor else ''}]"


def _media_descriptor(data: dict[str, Any]) -> str:
    for key in ("name", "file_name", "filename", "file", "file_id", "id"):
        value = data.get(key)
        if value not in (None, ""):
            text = str(value)
            if text.startswith(("http://", "https://", "data:", "base64://")):
                continue
            return text
    return ""


def _at_display_name(data: dict[str, Any]) -> str:
    for key in ("display_name", "name", "text"):
        value = str(data.get(key) or "").strip()
        if value.startswith("@"):
            value = value[1:].strip()
        if value and not value.isdigit():
            return value
    return ""


def cq_to_readable_text(text: str) -> str:
    return "".join(_segment_to_text(segment["type"], segment["data"]) for segment in message_to_segments(text))


def _unescape_cq(text: str, *, parameter: bool = False) -> str:
    if parameter:
        text = text.replace("&#44;", ",")
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw.lstrip(",").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key] = _unescape_cq(value, parameter=True)
    return params


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_timestamp(value: Any) -> int | None:
    number = _safe_int(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _event_hhmm(value: Any) -> str:
    timestamp = _safe_timestamp(value)
    if timestamp is None:
        return str(value or "")
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _response_ok(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("status") == "ok" and response.get("retcode") in (None, 0, "0")


def _is_unsupported_response(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    retcode = _safe_int(response.get("retcode"))
    wording = " ".join(str(response.get(key) or "") for key in ("wording", "message", "msg")).lower()
    return retcode in {1404, 404} or "不支持" in wording or "not support" in wording or "unsupported" in wording or "api not found" in wording


def _format_template(route: ForwardGroupConfig, nickname: str, sender_id: str, event_time: str, message: str) -> str:
    try:
        return route.forward_template.format(
            sender_nickname=nickname,
            sender_id=sender_id,
            time=event_time,
            message=message,
            source_group_id=route.source_group_id,
        )
    except Exception:
        return message


def _build_auth_headers() -> dict[str, str]:
    token = os.getenv("ONEBOT_ACCESS_TOKEN", os.getenv("SNOWLUMA_ACCESS_TOKEN", "")).strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _is_gray_event(event: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    if event.get("notice_type") == "group_upload":
        return False
    if str(event.get("post_type") or "") == "notice":
        return True
    if str(event.get("sub_type") or "").lower() in {"notice", "gray", "gray_tip", "system", "poke"}:
        return True
    return any(str(segment.get("type") or "").lower() in {"gray", "gray_tip", "notice", "poke"} for segment in segments)


def _gray_segments(event: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gray_parts = []
    for segment in segments:
        if segment.get("type") in {"gray", "gray_tip", "notice", "poke"}:
            gray_parts.append(_segment_to_text(str(segment.get("type")), segment.get("data") if isinstance(segment.get("data"), dict) else {}))
    text = "  ".join(gray_parts)
    if not text:
        notice_type = str(event.get("notice_type") or event.get("sub_type") or "系统消息")
        text = f"群通知：{notice_type}"
    return [{"type": "gray", "data": {"text": text}}]


def _is_qz_event(packet: dict[str, Any]) -> bool:
    values = (
        packet.get("post_type"),
        packet.get("notice_type"),
        packet.get("event_type"),
        packet.get("sub_type"),
        packet.get("type"),
    )
    return any("qzone" in str(value).lower() or str(value).lower() in {"qz", "dynamic", "dynamics"} for value in values)


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return ""


def _normalize_qz_data(data: Any, default_user_id: str) -> list[dict[str, Any]]:
    raw_items: list[Any]
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = []
        for key in ("msglist", "posts", "feeds", "dynamics", "dynamic", "items", "moments", "list", "entries"):
            value = data.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        if not raw_items and isinstance(data.get("data"), dict):
            return _normalize_qz_data(data["data"], default_user_id)
        if not raw_items and isinstance(data.get("data"), list):
            raw_items = data["data"]
        if not raw_items and any(key in data for key in ("content", "text", "message", "timestamp", "time", "created_time", "dynamic_id", "tid")):
            raw_items = [data]
    else:
        raw_items = []

    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raw = {"text": str(raw)}
        author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
        user_id = str(
            _first_value(raw, "user_id", "qq", "uin", "author_uin", "host_uin")
            or _first_value(author, "user_id", "qq", "uin")
            or default_user_id
        )
        source = raw.get("segments") if isinstance(raw.get("segments"), list) else raw.get("message")
        if source is None:
            source = raw.get("content", raw.get("text", raw.get("desc", "")))
        if isinstance(source, dict):
            source = source.get("text") or source.get("content") or source.get("desc") or ""
        segments = message_to_segments(source, raw.get("raw_message"))
        image_values = raw.get("images", raw.get("pics", raw.get("pictures", raw.get("pic", []))))
        if isinstance(image_values, dict):
            image_values = [image_values]
        if isinstance(image_values, list):
            for image in image_values:
                if isinstance(image, dict):
                    image_data = image.get("data") if isinstance(image.get("data"), dict) else image
                else:
                    image_data = {"url": str(image)}
                if image_data.get("url") or image_data.get("file"):
                    segments.append({"type": "image", "data": image_data})
        timestamp = _safe_timestamp(_first_value(raw, "timestamp", "created_time", "createdTime", "time", "create_time", "created_at"))
        message_id = _first_value(raw, "message_id", "dynamic_id", "tid", "cellid", "id")
        nickname = _first_value(raw, "nickname", "nick", "name", "uinname", "author_name") or _first_value(author, "nickname", "nick", "name")
        result.append(
            {
                "message_id": str(message_id) if message_id not in (None, "") else None,
                "timestamp": timestamp,
                "time": _event_hhmm(timestamp),
                "sender_id": user_id,
                "qz_user_id": user_id,
                "sender_nickname": str(nickname or user_id or "QQ 动态"),
                "segments": segments,
                "text": message_to_readable_text(segments),
            }
        )
    result.sort(key=lambda item: (item.get("timestamp") or 0, str(item.get("message_id") or "")))
    return result


def _dynamic_identity(item: dict[str, Any]) -> str:
    message_id = item.get("message_id")
    if message_id not in (None, ""):
        return str(message_id)
    content = f"{item.get('sender_id', '')}|{item.get('timestamp', '')}|{item.get('text', '')}"
    return hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()


def _unique_dynamics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        identity = _dynamic_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result
