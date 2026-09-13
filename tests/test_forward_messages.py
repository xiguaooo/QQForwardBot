import unittest
from unittest.mock import AsyncMock, patch
from PIL import Image
from io import BytesIO

from forward_messages import build_forward_nodes, resolve_forward_segments
from renderer import _build_blocks, render_messages_image


def forward(resource):
    return {"type": "forward", "data": {"id": resource}}


class ForwardMessagesTest(unittest.IsolatedAsyncioTestCase):
    async def test_nested_resources_and_official_nested_content(self):
        request = AsyncMock(side_effect=[
            {"retcode": "0", "data": {"messages": [{"sender": {"user_id": 123, "nickname": "Alice"}, "time": 1234, "group_id": 999, "content": [forward("inner")]}]}},
            {"data": {"message": [{"sender": {"user_id": 456, "nickname": "Bob"}, "content": [{"sender": {"nickname": "Carol"}, "content": "hello[CQ:face,id=14]"}]}]}},
        ])
        result = await resolve_forward_segments([forward("outer")], request)
        inner = result[0]["data"]["messages"][0]["content"][0]
        nested = inner["data"]["messages"][0]["content"][0]
        self.assertEqual(nested["type"], "node")
        self.assertEqual(nested["data"]["content"][1]["data"]["id"], "14")
        self.assertEqual(request.call_args_list[0].args, ("get_forward_msg", {"message_id": "outer"}))
        nodes = build_forward_nodes([{"render": {"sender_id": "99", "sender_nickname": "Root"}, "segments": result}])
        self.assertEqual(nodes[0]["data"]["content"][0]["data"]["name"], "Alice")
        self.assertEqual(nodes[0]["data"]["content"][0]["data"]["time"], 1234)
        self.assertEqual(result[0]["data"]["messages"][0]["group_id"], 999)
        self.assertNotIn("forward", str(nodes))
        with patch("renderer._load_face_segment", return_value=None):
            blocks = _build_blocks(result, None)
        self.assertEqual(blocks[0].kind, "quote")
        self.assertIn("Alice", blocks[0].value)

    async def test_cycle_and_failure_keep_resource_and_visible_text(self):
        request = AsyncMock(return_value={"data": {"messages": [{"content": [forward("loop")]}]}})
        result = await resolve_forward_segments([forward("loop")], request)
        cycle = result[0]["data"]["messages"][0]["content"][0]["data"]
        self.assertEqual(cycle["id"], "loop")
        self.assertEqual(cycle["error"], "循环引用")
        request.assert_awaited_once()
        failed = await resolve_forward_segments([forward("missing")], AsyncMock(side_effect=TimeoutError))
        self.assertEqual(failed[0]["data"]["id"], "missing")
        self.assertIn("读取失败", str(build_forward_nodes([{"segments": failed}])))

    async def test_depth_and_node_limit(self):
        request = AsyncMock(side_effect=lambda action, params, **kwargs: {"data": {"messages": [{"content": [forward(str(int(params["message_id"]) + 1))]}]}})
        resolved = await resolve_forward_segments([forward("0")], request)
        self.assertIn("嵌套深度限制", str(resolved))
        self.assertEqual(request.await_count, 5)
        many = AsyncMock(return_value={"data": {"messages": [{"content": "hello"}] * 205}})
        result = await resolve_forward_segments([forward("many")], many)
        self.assertEqual(len(result[0]["data"]["messages"]), 201)
        self.assertIn("节点数量限制", str(result))

    async def test_send_media_and_renderer_fields_removed(self):
        nodes = build_forward_nodes([{"render": {"sender_id": "123", "sender_nickname": "Author"}, "segments": [
            {"type": "image", "data": {"file": "backend-local", "url": "https://example.invalid/image", "render_image": "private"}},
            {"type": "at", "data": {"qq": "456", "name": "Display name"}},
            {"type": "gray", "data": {"text": "系统通知"}},
        ]}])
        content = nodes[0]["data"]["content"]
        self.assertEqual(nodes[0]["data"]["uin"], "123")
        self.assertEqual(content[0]["data"], {"file": "https://example.invalid/image"})
        self.assertEqual(content[1]["data"], {"qq": "456"})
        self.assertEqual(content[2], {"type": "text", "data": {"text": "系统通知"}})

    async def test_existing_quote_unchanged(self):
        blocks = _build_blocks([{"type": "reply", "data": {"id": 1}}, {"type": "text", "data": {"text": "reply"}}], {"sender_nickname": "Quoted", "text": "original"})
        self.assertEqual([block.kind for block in blocks], ["quote", "text"])
        self.assertEqual(blocks[0].children[0].value, "original")

    async def test_mixed_nested_nodes_preserve_order_and_file_link(self):
        text = lambda value: {"type": "text", "data": {"text": value}}
        nodes = build_forward_nodes([{"render": {"sender_id": "123", "sender_nickname": "Alice"}, "segments": [
            text("before"), {"type": "image", "data": {"url": "https://example.invalid/image"}},
            {"type": "node", "data": {"name": "Bob", "uin": "456", "content": [
                text("inner before"), {"type": "node", "data": {"name": "Carol", "uin": "789", "content": "deep"}}, text("inner after")
            ]}},
            text("after"), {"type": "file", "data": {"name": "report.txt", "url": "https://example.invalid/report"}},
        ]}])
        def check(content):
            if any(segment["type"] == "node" for segment in content):
                self.assertTrue(all(segment["type"] == "node" for segment in content))
                for segment in content:
                    check(segment["data"]["content"])
        check(nodes)
        content = nodes[0]["data"]["content"]
        self.assertEqual([node["data"]["name"] for node in content], ["Alice", "Bob", "Alice"])
        self.assertEqual(content[0]["data"]["content"][0], text("before"))
        self.assertEqual(content[0]["data"]["content"][1]["type"], "image")
        self.assertEqual(content[2]["data"]["content"][0], text("after"))
        self.assertEqual(content[2]["data"]["content"][1], text("[文件：report.txt] https://example.invalid/report"))

    async def test_nested_forward_renders_png_without_network(self):
        segments = await resolve_forward_segments([
            {"type": "node", "data": {"name": "Alice", "uin": "123", "time": 1234, "content": [
                {"type": "node", "data": {"name": "Bob", "uin": "456", "content": "nested content"}}
            ]}}
        ], AsyncMock())
        with patch("renderer._load_avatar", return_value=None):
            png = render_messages_image(group_name="Test", messages=[{"sender_id": "123", "segments": segments}])
        with Image.open(BytesIO(png)) as rendered:
            self.assertEqual(rendered.format, "PNG")
            self.assertGreater(rendered.height, 100)


if __name__ == "__main__":
    unittest.main()
