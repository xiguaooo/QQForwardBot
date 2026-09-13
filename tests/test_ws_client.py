import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from websockets.legacy.server import serve
from websockets.legacy.client import connect

from config import AppConfig, ForwardGroupConfig
from dataclasses import replace
from ws_client import (RuntimeState, SnowLumaForwarder, message_to_segments,
                       message_to_readable_text, _build_auth_headers, _is_gray_event,
                       _is_unsupported_response)


def ok(data):
    return {"status": "ok", "retcode": 0, "data": data}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.route = ForwardGroupConfig("test", "测试", 123, (456,), forward_mode="forward")
        cfg = AppConfig(groups=(self.route,), backend="go-cqhttp")
        self.client = SnowLumaForwarder(SimpleNamespace(get=lambda: cfg), RuntimeState())
        self.client.state.backend = "go-cqhttp"

    def test_cq_unescape_is_contextual_and_single_pass(self):
        segments = message_to_segments("&amp;#91; &#91;字&#93; &#44;[CQ:image,file=x,url=https://e.test/?a=1&amp;b=2&#44;3]")
        self.assertEqual(segments[0]["data"]["text"], "&#91; [字] &#44;")
        self.assertEqual(segments[1]["data"]["url"], "https://e.test/?a=1&b=2,3")
        literal = [{"type": "text", "data": {"text": "&amp;"}}]
        self.assertEqual(message_to_readable_text(literal), "&amp;")

    def test_token_precedence_and_generic_api_error(self):
        with patch.dict(os.environ, {"ONEBOT_ACCESS_TOKEN": "new", "SNOWLUMA_ACCESS_TOKEN": "old"}):
            self.assertEqual(_build_auth_headers(), {"Authorization": "Bearer new"})
        self.assertFalse(_is_unsupported_response({"retcode": 1200, "message": "login expired"}))
        self.assertTrue(_is_unsupported_response({"retcode": 404}))

    async def test_history_pages_by_sequence_and_limits_result(self):
        async def request(action, params, **kw):
            self.assertEqual(action, "get_group_msg_history")
            self.assertNotIn("count", params)
            end = params.get("message_seq", 60)
            return ok({"messages": [
                {"message_id": -seq, "message_seq": seq, "time": 100,
                 "message": str(seq), "sender": {"user_id": 42}}
                for seq in range(end - 19, end + 1)
            ]})
        self.client._request_action = AsyncMock(side_effect=request)
        history = await self.client._fetch_group_history(123, 50)
        self.assertEqual([x["message_seq"] for x in history], list(range(11, 61)))
        calls = self.client._request_action.call_args_list
        self.assertEqual([c.args[1].get("message_seq") for c in calls], [None, 40, 20])

    async def test_history_backend_ignoring_cursor_does_not_loop(self):
        self.client._request_action = AsyncMock(return_value=ok({"messages": [
            {"message_id": 2, "message_seq": 20, "time": 10, "message": "a"}]}))
        self.assertEqual(len(await self.client._fetch_group_history(123, 50)), 1)
        self.assertEqual(self.client._request_action.await_count, 2)

    async def test_group_file_notice_uses_download_then_upload(self):
        self.client._request_action = AsyncMock(side_effect=[
            ok({"url": "https://example.test/file"}),
            ok({"file": "/backend/cache/file"}), ok(None),
        ])
        event = {"post_type": "notice", "notice_type": "group_upload",
                 "file": {"id": "abc", "name": "文件.zip", "busid": 102}}
        segments = await self.client._notice_segments(self.route, event)
        self.assertFalse(_is_gray_event(event, segments))
        await self.client._submit_media(456, segments[0], {})
        calls = self.client._request_action.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["get_group_file_url", "download_file", "upload_group_file"])
        self.assertEqual(calls[-1].args[1]["file"], "/backend/cache/file")
        self.assertEqual(self.client.state.forward_records[-1]["status"], "success")

    async def test_unknown_send_is_recorded_once_without_retry(self):
        self.client._request_action = AsyncMock(return_value=None)
        await self.client._submit_group_forward(456, [], {})
        self.assertEqual(self.client._request_action.await_count, 1)
        self.assertEqual(self.client.state.forward_records[-1]["status"], "unknown")
        self.assertFalse(self.client._pending_actions)

    async def test_media_prefers_remote_url_and_go_cq_skips_avatar_extension(self):
        self.client._request_action = AsyncMock()
        image = {"type": "image", "data": {"file": "cache.image", "url": "https://example.test/image"}}
        self.assertEqual(await self.client._resolve_media_segments([image]), [image])
        self.assertIsNone(await self.client._resolve_avatar("123"))
        self.client._request_action.assert_not_awaited()

    async def test_image_mode_receives_expanded_nested_content(self):
        route = replace(self.route, forward_mode="image")
        self.client._fetch_group_member_info = AsyncMock(return_value={})
        self.client._fetch_group_info = AsyncMock(return_value={"group_name": "源群"})
        self.client._request_action = AsyncMock(return_value=ok({"messages": [
            {"sender": {"user_id": 42, "nickname": "作者"}, "time": 1700000000,
             "content": "合并内容"}]}))
        self.client._submit_group_message = AsyncMock()
        event = {"user_id": 43, "message_id": 1, "time": 1700000000,
                 "message": "[CQ:forward,id=resource]"}
        with patch("ws_client.render_message_history_image", return_value=b"png") as render:
            await self.client._handle_source_event(event, route)
        segment = render.call_args.kwargs["messages"][0]["segments"][0]
        self.assertEqual(segment["data"]["messages"][0]["content"][0]["data"]["text"], "合并内容")
        outgoing = self.client._submit_group_message.call_args.args[1]
        self.assertEqual(outgoing, [{"type": "image", "data": {"file": "base64://cG5n"}}])

    async def test_native_file_is_also_uploaded(self):
        item = {"render": {"sender_id": "42", "sender_nickname": "作者"},
                "segments": [{"type": "file", "data": {"name": "test.zip", "url": "https://e.test/f"}}]}
        self.client._submit_group_forward = AsyncMock()
        self.client._submit_media = AsyncMock()
        await self.client._send_batch(self.route, [item], content_type="rendered_image", command_label=None)
        self.client._submit_media.assert_awaited_once()
        nodes = self.client._submit_group_forward.call_args.args[1]
        self.assertEqual(nodes[0]["data"]["content"][0]["type"], "text")

    async def test_batches_preserve_order_while_first_send_is_slow(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        self.client._fetch_group_member_info = AsyncMock(return_value={})

        async def send(route, items, **kwargs):
            order.append(items[0]["message_id"])
            if items[0]["message_id"] == 1:
                entered.set()
                await release.wait()

        self.client._send_batch = send
        first = asyncio.create_task(self.client._handle_source_event({"message": "a", "message_id": 1}, self.route))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.client._handle_source_event({"message": "b", "message_id": 2}, self.route))
        await asyncio.sleep(0)
        self.assertEqual(order, [1])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(order, [1, 2])

    async def test_websocket_event_to_native_forward_contract(self):
        sent = []
        done = asyncio.Event()

        async def backend(ws):
            await ws.send(json.dumps({"post_type": "message", "message_type": "group",
                "group_id": 123, "self_id": 999, "user_id": 42, "message_id": -12,
                "time": 1700000000, "sender": {"nickname": "外层"},
                "message": "前言[CQ:forward,id=res-1]后记"}))
            async for raw in ws:
                packet = json.loads(raw)
                action = packet["action"]
                if action == "get_version_info":
                    data = {"app_name": "go-cqhttp"}
                elif action == "get_login_info":
                    data = {"user_id": 999}
                elif action == "get_group_member_info":
                    data = {"nickname": "外层"}
                elif action == "get_forward_msg":
                    self.assertEqual(packet["params"], {"message_id": "res-1"})
                    data = {"messages": [{"sender": {"user_id": 43, "nickname": "内层"},
                        "time": 1700000000, "content": "正文&amp;文字"}]}
                elif action == "send_group_forward_msg":
                    sent.append(packet)
                    data = {"message_id": 77, "forward_id": "sent-resource"}
                else:
                    self.fail(f"Unexpected action: {action}")
                await ws.send(json.dumps({**ok(data), "echo": packet["echo"]}))
                if action == "send_group_forward_msg":
                    done.set()

        async with serve(backend, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/") as ws:
                self.client._ws = ws
                self.client.state.backend = "auto"
                receiver = asyncio.create_task(self.client._receive_loop(ws))
                try:
                    await self.client._initialize_backend()
                    await asyncio.wait_for(done.wait(), 5)
                    if self.client._message_tasks:
                        await asyncio.wait_for(asyncio.gather(*list(self.client._message_tasks)), 5)
                finally:
                    await self.client.stop()
                    await receiver
        self.assertEqual(self.client.state.backend, "go-cqhttp")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["params"]["group_id"], 456)
        serialized = json.dumps(sent[0]["params"]["messages"], ensure_ascii=False)
        for text in ("外层", "内层", "前言", "后记", "正文&文字"):
            self.assertIn(text, serialized)
        self.assertNotIn('"type": "forward"', serialized)
        self.assertEqual(self.client.state.forward_records[-1]["forward_id"], "sent-resource")
        self.assertFalse(self.client._pending_requests)


if __name__ == "__main__":
    unittest.main()
