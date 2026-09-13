from __future__ import annotations

import base64
import io
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


CANVAS_WIDTH = 368
TOP_BAR_HEIGHT = 52
BACKGROUND_COLOR = (243, 244, 250)
INCOMING_BUBBLE = (255, 255, 255)
OUTGOING_BUBBLE = (55, 187, 238)
BUBBLE_SHADOW = (227, 230, 239)
MAX_BUBBLE_WIDTH = 244
MAX_CONTENT_WIDTH = MAX_BUBBLE_WIDTH - 28
AVATAR_SIZE = 40
BODY_LINE_HEIGHT = 23
MAX_TEXT_LENGTH = 6000
TOP_BAR_COLORS = ((0, 211, 246), (0, 180, 241), (17, 145, 235))


@dataclass
class RenderBlock:
    kind: str
    value: Any = None
    images: list[Image.Image] | None = None
    children: list["RenderBlock"] | None = None


def render_message_image(
    *,
    sender_nickname: str,
    sender_id: str,
    event_time: str,
    group_name: str,
    segments: list[dict[str, Any]],
    member_title: str = "",
    role: str = "member",
    quote: dict[str, Any] | None = None,
    outgoing: bool = False,
    group_member_count: int | None = None,
    avatar: Any = None,
) -> bytes:
    return render_messages_image(
        group_name=group_name,
        group_member_count=group_member_count,
        messages=[
            {
                "sender_nickname": sender_nickname,
                "sender_id": sender_id,
                "event_time": event_time,
                "segments": segments,
                "member_title": member_title,
                "role": role,
                "quote": quote,
                "outgoing": outgoing,
                "avatar": avatar,
            }
        ],
    )


def render_message_history_image(
    *,
    group_name: str,
    messages: list[dict[str, Any]],
    group_member_count: int | None = None,
) -> bytes:
    return render_messages_image(
        group_name=group_name,
        group_member_count=group_member_count,
        messages=messages,
    )


def render_messages_image(
    *,
    group_name: str,
    messages: list[dict[str, Any]],
    group_member_count: int | None = None,
) -> bytes:
    body_font = _font(16)
    quote_font = _font(12)
    meta_font = _font(10)
    name_font = _font(12, bold=True)
    time_font = _font(10)
    gray_font = _font(12)
    prepared: list[dict[str, Any]] = []

    for message in messages:
        segments = message.get("segments")
        blocks = _build_blocks(segments if isinstance(segments, list) else [], message.get("quote"))
        measured = [_measure_block(block, body_font, quote_font) for block in blocks]
        is_gray = bool(message.get("is_gray")) or any(block.kind in {"gray", "poke"} for block in blocks)
        if is_gray:
            gray_text = "  ".join(str(block.value or "") for block in blocks if block.kind in {"gray", "poke"}) or "系统消息"
            gray_lines = _wrap_text(gray_text, gray_font, CANVAS_WIDTH - 42)
            row_height = 20 + len(gray_lines) * 18 + 12
            prepared.append(
                {
                    "message": message,
                    "blocks": blocks,
                    "measured": measured,
                    "is_gray": True,
                    "gray_text": gray_text,
                    "bubble_width": 0,
                    "bubble_height": 0,
                    "row_height": row_height,
                }
            )
            continue

        content_height = sum(height for _, height in measured) + max(0, len(measured) - 1) * 7
        content_width = max((width for width, _ in measured), default=60)
        bubble_width = min(MAX_BUBBLE_WIDTH, max(78, content_width + 28))
        bubble_height = max(44, content_height + 24)
        prepared.append(
            {
                "message": message,
                "blocks": blocks,
                "measured": measured,
                "is_gray": False,
                "bubble_width": bubble_width,
                "bubble_height": bubble_height,
                "row_height": 16 + 18 + bubble_height + 18,
            }
        )

    if not prepared:
        empty_message = {
            "sender_nickname": "OneBot 协议端",
            "sender_id": "",
            "event_time": "",
            "segments": [{"type": "text", "data": {"text": "[空消息]"}}],
        }
        blocks = [RenderBlock("text", "[空消息]")]
        prepared = [
            {
                "message": empty_message,
                "blocks": blocks,
                "measured": [_measure_block(blocks[0], body_font, quote_font)],
                "is_gray": False,
                "bubble_width": 100,
                "bubble_height": 48,
                "row_height": 100,
            }
        ]

    canvas_height = TOP_BAR_HEIGHT + 12 + sum(item["row_height"] for item in prepared) + 12
    canvas = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(canvas)
    _draw_top_bar(draw, group_name, group_member_count)

    cursor = TOP_BAR_HEIGHT + 10
    for item in prepared:
        message = item["message"]
        if item["is_gray"]:
            _draw_gray_row(draw, item["gray_text"], cursor, gray_font)
            cursor += item["row_height"]
            continue

        _draw_centered_text(draw, str(message.get("event_time") or ""), cursor, time_font, (139, 146, 163))
        header_y = cursor + 16
        bubble_y = header_y + 18
        outgoing = bool(message.get("outgoing"))
        bubble_width = int(item["bubble_width"])
        bubble_height = int(item["bubble_height"])
        if outgoing:
            bubble_right = 306
            bubble_x = bubble_right - bubble_width
            avatar_x = 314
        else:
            bubble_x = 63
            avatar_x = 14

        bubble_color = OUTGOING_BUBBLE if outgoing else INCOMING_BUBBLE
        draw.rounded_rectangle(
            (bubble_x, bubble_y + 2, bubble_x + bubble_width, bubble_y + bubble_height + 2),
            radius=15,
            fill=BUBBLE_SHADOW,
        )
        draw.rounded_rectangle(
            (bubble_x, bubble_y, bubble_x + bubble_width, bubble_y + bubble_height),
            radius=15,
            fill=bubble_color,
        )
        if outgoing:
            draw.polygon(
                [(bubble_x + bubble_width - 3, bubble_y + 12), (bubble_x + bubble_width + 9, bubble_y + 18), (bubble_x + bubble_width - 3, bubble_y + 25)],
                fill=bubble_color,
            )
        else:
            draw.polygon(
                [(bubble_x + 3, bubble_y + 12), (bubble_x - 9, bubble_y + 18), (bubble_x + 3, bubble_y + 25)],
                fill=bubble_color,
            )

        avatar = _load_avatar(str(message.get("sender_id") or ""), message.get("avatar"))
        _draw_avatar(canvas, avatar, avatar_x, bubble_y, str(message.get("sender_id") or ""))
        _draw_header(draw, message, bubble_x, header_y, bubble_width, name_font, meta_font, outgoing)

        content_x = bubble_x + 14
        content_y = bubble_y + 12
        content_width = bubble_width - 28
        text_color = (255, 255, 255) if outgoing else (35, 41, 54)
        for index, block in enumerate(item["blocks"]):
            _draw_block(draw, canvas, block, content_x, content_y, body_font, quote_font, text_color, content_width)
            content_y += item["measured"][index][1] + (7 if index < len(item["blocks"]) - 1 else 0)
        cursor += item["row_height"]

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _draw_top_bar(draw: ImageDraw.ImageDraw, group_name: str, group_member_count: int | None) -> None:
    for x in range(CANVAS_WIDTH):
        ratio = x / max(1, CANVAS_WIDTH - 1)
        position = ratio * (len(TOP_BAR_COLORS) - 1)
        left = min(int(position), len(TOP_BAR_COLORS) - 2)
        local = position - left
        start = TOP_BAR_COLORS[left]
        end = TOP_BAR_COLORS[left + 1]
        color = tuple(int(start[i] + (end[i] - start[i]) * local) for i in range(3))
        draw.line((x, 0, x, TOP_BAR_HEIGHT), fill=color)

    icon_color = (235, 251, 255)
    draw.line((24, 17, 18, 26), fill=icon_color, width=2)
    draw.line((18, 26, 24, 35), fill=icon_color, width=2)
    draw.line((18, 26, 31, 26), fill=icon_color, width=2)
    draw.line((342, 18, 356, 18), fill=icon_color, width=2)
    draw.line((342, 26, 356, 26), fill=icon_color, width=2)
    draw.line((342, 34, 356, 34), fill=icon_color, width=2)

    title = str(group_name or "QQ 群")
    if group_member_count and group_member_count > 0:
        title = f"{title}（{group_member_count}）"
    title_font = _font(17, bold=True)
    title = _fit_text(title, title_font, 290)
    box = draw.textbbox((0, 0), title, font=title_font)
    title_y = max(5, (TOP_BAR_HEIGHT - (box[3] - box[1])) // 2 - box[1])
    _draw_text(draw, ((CANVAS_WIDTH - _text_length(title, title_font)) / 2, title_y), title, title_font, "white")


def _draw_header(
    draw: ImageDraw.ImageDraw,
    message: dict[str, Any],
    bubble_x: int,
    y: int,
    bubble_width: int,
    name_font: ImageFont.FreeTypeFont,
    meta_font: ImageFont.FreeTypeFont,
    outgoing: bool,
) -> None:
    role_label, role_background, role_foreground = _role_style(str(message.get("role") or "member"))
    badge_text = _fit_text(str(message.get("member_title") or role_label), meta_font, 58)
    badge_width = int(_text_length(badge_text, meta_font)) + 12
    nickname = _fit_text(str(message.get("sender_nickname") or message.get("sender_id") or "未知用户"), name_font, 104)
    account = f"QQ {message.get('sender_id') or ''}".strip()
    account = _fit_text(account, meta_font, 72) if account else ""
    total_width = int(_text_length(nickname, name_font)) + 4 + badge_width + (5 + int(_text_length(account, meta_font)) if account else 0)
    start_x = bubble_x + bubble_width - total_width if outgoing else bubble_x
    start_x = max(8, min(start_x, CANVAS_WIDTH - total_width - 8))

    _draw_text(draw, (start_x, y + 1), nickname, name_font, (104, 111, 128))
    badge_x = start_x + _text_length(nickname, name_font) + 4
    draw.rounded_rectangle((badge_x, y, badge_x + badge_width, y + 17), radius=4, fill=role_background)
    _draw_text(draw, (badge_x + 6, y + 3), badge_text, meta_font, role_foreground)
    if account:
        account_x = badge_x + badge_width + 5
        _draw_text(draw, (account_x, y + 3), account, meta_font, (154, 160, 174))


def _draw_gray_row(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont) -> None:
    lines = _wrap_text(text, font, CANVAS_WIDTH - 42)
    for index, line in enumerate(lines):
        _draw_centered_text(draw, line, y + 7 + index * 18, font, (146, 153, 168))


def _build_blocks(segments: list[dict[str, Any]], quote: dict[str, Any] | None) -> list[RenderBlock]:
    blocks: list[RenderBlock] = []
    text_parts: list[str] = []
    quote_rendered = False

    def flush_text() -> None:
        if text_parts:
            text = "".join(text_parts).strip()
            if text:
                blocks.append(RenderBlock("text", text[:MAX_TEXT_LENGTH]))
            text_parts.clear()

    for segment in segments:
        if not isinstance(segment, dict):
            text_parts.append(str(segment))
            continue
        seg_type = str(segment.get("type", "unknown")).lower()
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if seg_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = data.get("qq", "")
            text_parts.append("@全体成员" if str(qq) == "all" else f"@{_at_display_name(data) or qq}")
        elif seg_type in {"forward", "node"}:
            flush_text()
            from forward_messages import _node
            nodes = data.get("messages") if seg_type == "forward" else [data]
            if not isinstance(nodes, list) or not nodes:
                blocks.append(RenderBlock("text", f"[合并转发 {data.get('id', '')}：内容不可用]"))
                continue
            for original in nodes:
                node = _node(original)
                sender = node["sender"]
                heading = f"{sender['nickname']}  {node['time']}".strip()
                blocks.append(RenderBlock("quote", heading, children=_build_blocks(node["content"], None)))
        elif seg_type == "reply":
            if quote is None or quote_rendered:
                continue
            flush_text()
            quote_segments = quote.get("segments") if isinstance(quote, dict) else None
            if isinstance(quote_segments, list):
                quote_segments = [
                    child
                    for child in quote_segments
                    if not isinstance(child, dict) or str(child.get("type", "")).lower() != "reply"
                ]
                quote_children = _build_blocks(quote_segments, None)
            else:
                quote_text = str(quote.get("text") or "引用消息不可用")
                quote_children = [RenderBlock("text", quote_text)]
            blocks.append(RenderBlock("quote", _quote_text(quote, data.get("id")), children=quote_children))
            quote_rendered = True
        elif seg_type == "image":
            flush_text()
            image = _load_image_segment(data)
            blocks.append(RenderBlock("image", image) if image is not None else RenderBlock("text", "[图片无法加载]"))
        elif seg_type == "face":
            flush_text()
            blocks.append(RenderBlock("image", _load_face_segment(data)))
        elif seg_type == "record":
            flush_text()
            blocks.append(RenderBlock("record", data))
        elif seg_type == "video":
            flush_text()
            blocks.append(RenderBlock("video", data))
        elif seg_type == "file":
            flush_text()
            blocks.append(RenderBlock("file", data))
        elif seg_type in {"gray", "gray_tip", "notice"}:
            flush_text()
            blocks.append(RenderBlock("gray", data.get("text") or data.get("message") or "系统消息"))
        elif seg_type == "poke":
            flush_text()
            blocks.append(RenderBlock("poke", data.get("text") or "戳一戳"))
        elif seg_type == "json":
            text_parts.append("[JSON 卡片]")
        elif seg_type == "xml":
            text_parts.append("[XML 卡片]")
        else:
            text_parts.append(f"[{seg_type}]")

    flush_text()
    if not blocks:
        blocks.append(RenderBlock("text", "[空消息]"))
    return blocks


def _measure_block(
    block: RenderBlock,
    body_font: ImageFont.FreeTypeFont,
    quote_font: ImageFont.FreeTypeFont,
    max_width: int = MAX_CONTENT_WIDTH,
) -> tuple[int, int]:
    if block.kind == "image" and isinstance(block.value, Image.Image):
        return _fit_size(block.value.width, block.value.height, max_width, 300)
    if block.kind == "quote":
        children = block.children or [RenderBlock("text", "引用消息不可用")]
        child_measurements = [
            _measure_block(child, quote_font, quote_font, max(30, max_width - 20))
            for child in children
        ]
        height = 10 + 17 + 7 + sum(child_height for _, child_height in child_measurements)
        height += max(0, len(child_measurements) - 1) * 5 + 9
        return max_width, height
    if block.kind == "record":
        transcript = _media_text(block.value, "text") or _media_text(block.value, "transcription")
        return min(max_width, 220), 78 if transcript else 52
    if block.kind == "video":
        return min(max_width, 220), 42
    if block.kind == "file":
        return min(max_width, 220), 38
    if block.kind in {"gray", "poke"}:
        return 0, 18
    lines = _wrap_text(str(block.value or ""), body_font, MAX_CONTENT_WIDTH)
    width = max(42, min(MAX_CONTENT_WIDTH, int(max((_text_length(line, body_font) for line in lines), default=42))))
    return width, max(BODY_LINE_HEIGHT, len(lines) * BODY_LINE_HEIGHT)


def _draw_block(
    draw: ImageDraw.ImageDraw,
    canvas: Image.Image,
    block: RenderBlock,
    x: int,
    y: int,
    body_font: ImageFont.FreeTypeFont,
    quote_font: ImageFont.FreeTypeFont,
    text_color: Any,
    content_width: int,
) -> None:
    if block.kind == "image" and isinstance(block.value, Image.Image):
        width, height = _fit_size(block.value.width, block.value.height, content_width, 300)
        image = block.value.resize((width, height), Image.Resampling.LANCZOS)
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=9, fill=255)
        canvas.paste(image, (x, y), mask)
        return

    if block.kind == "quote":
        children = block.children or [RenderBlock("text", "引用消息不可用")]
        child_measurements = [
            _measure_block(child, quote_font, quote_font, max(30, content_width - 20))
            for child in children
        ]
        height = 10 + 17 + 7 + sum(child_height for _, child_height in child_measurements)
        height += max(0, len(child_measurements) - 1) * 5 + 9
        draw.rounded_rectangle((x, y, x + content_width, y + height), radius=9, fill=(238, 239, 245))
        _draw_text(draw, (x + 10, y + 7), str(block.value or "引用消息"), quote_font, (92, 98, 114))
        line_y = y + 27
        for index, child in enumerate(children):
            _draw_block(
                draw,
                canvas,
                child,
                x + 10,
                line_y,
                quote_font,
                quote_font,
                (62, 69, 83),
                max(30, content_width - 20),
            )
            line_y += child_measurements[index][1] + (5 if index < len(children) - 1 else 0)
        return

    if block.kind == "record":
        transcript = _media_text(block.value, "text") or _media_text(block.value, "transcription")
        duration = _media_text(block.value, "duration") or _media_text(block.value, "length")
        dark = text_color != (255, 255, 255)
        ink = (70, 76, 89) if dark else (239, 251, 255)
        play_fill = (73, 80, 94) if dark else (255, 255, 255)
        play_ink = (255, 255, 255) if dark else OUTGOING_BUBBLE
        draw.ellipse((x, y + 4, x + 24, y + 28), fill=play_fill)
        draw.polygon([(x + 10, y + 10), (x + 10, y + 22), (x + 17, y + 16)], fill=play_ink)
        for index in range(25):
            bar_height = 5 + ((index * 13) % 15)
            bar_x = x + 34 + index * 6
            if bar_x >= x + content_width - 26:
                break
            draw.line((bar_x, y + 16 - bar_height // 2, bar_x, y + 16 + bar_height // 2), fill=ink, width=1)
        if duration:
            _draw_text(draw, (x + content_width - _text_length(duration, quote_font), y + 8), duration, quote_font, ink)
        if transcript:
            draw.line((x, y + 36, x + content_width, y + 36), fill=(222, 225, 232), width=1)
            for index, line in enumerate(_wrap_text(transcript, body_font, content_width)):
                _draw_text(draw, (x, y + 43 + index * 18), line, body_font, text_color)
        return

    if block.kind == "video":
        descriptor = _media_text(block.value, "name") or _media_text(block.value, "file") or "视频"
        draw.rounded_rectangle((x, y + 2, x + min(content_width, 220), y + 40), radius=8, fill=(221, 224, 229))
        _draw_text(draw, (x + 10, y + 12), f"视频  {_fit_text(descriptor, quote_font, max(30, content_width - 58))}", quote_font, (92, 98, 108))
        return

    if block.kind == "file":
        descriptor = _media_text(block.value, "name") or _media_text(block.value, "file") or "未命名文件"
        draw.rounded_rectangle((x, y + 2, x + min(content_width, 220), y + 36), radius=8, fill=(240, 242, 246))
        _draw_text(draw, (x + 10, y + 10), f"文件  {_fit_text(descriptor, quote_font, max(30, content_width - 54))}", quote_font, (75, 82, 96))
        return

    lines = _wrap_text(str(block.value or ""), body_font, content_width)
    for index, line in enumerate(lines):
        _draw_text(draw, (x, y + index * BODY_LINE_HEIGHT), line, body_font, text_color)


def _quote_text(quote: dict[str, Any] | None, reply_id: Any) -> str:
    if not quote:
        return f"消息 #{reply_id} ›"
    sender = str(quote.get("sender_nickname") or quote.get("sender_id") or "未知用户")
    event_time = str(quote.get("time") or "")
    header = " ".join(part for part in (sender, event_time) if part)
    return f"{header} ›" if header else "引用消息 ›"


def _load_images(segments: Any) -> list[Image.Image]:
    images: list[Image.Image] = []
    if not isinstance(segments, list):
        return images
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        image = _load_image_segment(data)
        if image is not None:
            images.append(image)
    return images[:4]


def _load_image_segment(data: dict[str, Any]) -> Image.Image | None:
    sources: list[tuple[str, Any]] = []
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    for source_data in (data, nested):
        for key in ("base64", "url", "temp_url", "file", "path", "file_path", "file_url"):
            if key in source_data:
                sources.append((key, source_data.get(key)))
    for key, value in sources:
        if value in (None, ""):
            continue
        source = str(value)
        if key == "base64" and not source.startswith(("base64://", "data:")):
            source = "base64://" + source
        raw = _read_source(source)
        if raw is None:
            continue
        try:
            with Image.open(io.BytesIO(raw)) as image:
                return image.convert("RGB")
        except Exception:
            continue
    return None


def _load_face_segment(data: dict[str, Any]) -> Image.Image:
    face_id = data.get("id")
    raw = data.get("raw")
    if face_id in (None, "") and isinstance(raw, dict):
        face_id = raw.get("faceIndex") or raw.get("id")
    face_id = str(face_id or "")
    for url in (
        str(data.get("url") or ""),
        f"https://qzonestyle.gtimg.cn/qzone/em/e{face_id}.gif",
        f"https://gxh.vip.qq.com/club/item/parcel/item/0/face/{face_id}.png",
    ):
        if not url:
            continue
        image = _load_image_url(url)
        if image is not None:
            return image
    return _face_placeholder(face_id)


def _face_placeholder(face_id: str) -> Image.Image:
    image = Image.new("RGB", (32, 32), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 29, 29), fill=(255, 215, 75), outline=(224, 168, 34), width=1)
    draw.ellipse((10, 11, 12, 14), fill=(93, 76, 34))
    draw.ellipse((20, 11, 22, 14), fill=(93, 76, 34))
    draw.arc((10, 13, 22, 24), 15, 165, fill=(121, 76, 34), width=1)
    return image


@lru_cache(maxsize=256)
def _load_image_url(url: str) -> Image.Image | None:
    raw = _read_source(url)
    if raw is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            return image.convert("RGB")
    except Exception:
        return None


def _read_source(value: str) -> bytes | None:
    if value.startswith("base64://"):
        try:
            return base64.b64decode(value[9:], validate=False)
        except (ValueError, base64.binascii.Error):
            return None
    if value.startswith("data:") and "," in value:
        try:
            header, encoded = value.split(",", 1)
            return base64.b64decode(encoded, validate=False) if ";base64" in header else urllib.parse.unquote_to_bytes(encoded)
        except (ValueError, base64.binascii.Error):
            return None
    if value.startswith(("http://", "https://")):
        try:
            request = urllib.request.Request(value, headers={"User-Agent": "onebot-forwarder/1.0"})
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.read(12 * 1024 * 1024)
        except Exception:
            return None
    if value.startswith("file://"):
        parsed = urllib.parse.urlparse(value)
        value = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
            value = value[1:]
    try:
        path = Path(value)
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


@lru_cache(maxsize=256)
def _download_avatar(user_id: str) -> bytes | None:
    for url in (
        f"https://q1.qlogo.cn/g?b=qq&nk={urllib.parse.quote(user_id)}&s=640",
        f"https://thirdqq.qlogo.cn/g?b=qq&nk={urllib.parse.quote(user_id)}&s=640",
    ):
        raw = _read_source(url)
        if raw:
            return raw
    return None


def _load_avatar(user_id: str, source: Any = None) -> Image.Image | None:
    sources: list[Any] = []
    if isinstance(source, dict):
        nested = source.get("data") if isinstance(source.get("data"), dict) else {}
        for source_data in (source, nested):
            for key in ("base64", "url", "temp_url", "file", "path", "file_path", "file_url"):
                value = source_data.get(key)
                if value not in (None, ""):
                    sources.append(value)
    elif source not in (None, ""):
        sources.append(source)

    for value in sources:
        raw = _read_source(str(value))
        if not raw:
            continue
        try:
            with Image.open(io.BytesIO(raw)) as image:
                return image.convert("RGB")
        except Exception:
            continue

    raw = _download_avatar(str(user_id)) if user_id else None
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            return image.convert("RGB")
    except Exception:
        return None


def _draw_avatar(canvas: Image.Image, avatar: Image.Image | None, x: int, y: int, user_id: str) -> None:
    if avatar is None:
        avatar = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), (151, 183, 220))
        draw = ImageDraw.Draw(avatar)
        initials = str(user_id)[-2:] or "QQ"
        font = _font(12, bold=True)
        box = draw.textbbox((0, 0), initials, font=font)
        _draw_text(draw, ((AVATAR_SIZE - (box[2] - box[0])) / 2, (AVATAR_SIZE - (box[3] - box[1])) / 2 - 3), initials, font, "white")
    else:
        avatar = ImageOps.fit(avatar, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), fill=255)
    canvas.paste(avatar, (x, y), mask)


def _role_style(role: str) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    normalized = str(role or "member").lower()
    if normalized in {"owner", "creator"}:
        return "群主", (238, 177, 34), (255, 255, 255)
    if normalized in {"admin", "administrator"}:
        return "管理员", (48, 187, 132), (255, 255, 255)
    return "用户", (143, 101, 204), (255, 255, 255)


@lru_cache(maxsize=24)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "C:\\Windows\\Fonts\\msyhbd.ttc" if bold else "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=8)
def _emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    candidates = (
        "C:\\Windows\\Fonts\\seguiemj.ttf",
        "C:\\Windows\\Fonts\\seguisym.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return None


def _is_emoji(character: str) -> bool:
    codepoint = ord(character)
    return 0x1F000 <= codepoint <= 0x1FAFF or 0x1FC00 <= codepoint <= 0x1FFFF or 0x2300 <= codepoint <= 0x23FF or 0x2600 <= codepoint <= 0x27BF or 0xFE00 <= codepoint <= 0xFE0F or codepoint == 0x200D


def _text_runs(text: str, font: ImageFont.FreeTypeFont) -> list[tuple[str, ImageFont.FreeTypeFont, bool]]:
    runs: list[tuple[str, ImageFont.FreeTypeFont, bool]] = []
    current_text = ""
    current_font = font
    current_is_emoji = False
    for character in text:
        emoji_font = _emoji_font(getattr(font, "size", 16)) if _is_emoji(character) else None
        next_font = emoji_font or font
        next_is_emoji = emoji_font is not None
        if current_text and (next_font != current_font or next_is_emoji != current_is_emoji):
            runs.append((current_text, current_font, current_is_emoji))
            current_text = ""
        current_text += character
        current_font = next_font
        current_is_emoji = next_is_emoji
    if current_text:
        runs.append((current_text, current_font, current_is_emoji))
    return runs


def _text_length(text: str, font: ImageFont.FreeTypeFont) -> float:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return sum(draw.textlength(run, font=run_font) for run, run_font, _ in _text_runs(text, font))


def _draw_text(draw: ImageDraw.ImageDraw, position: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill: Any) -> None:
    x, y = position
    for run, run_font, is_emoji in _text_runs(text, font):
        try:
            draw.text((x, y), run, font=run_font, fill=fill, embedded_color=is_emoji)
        except (TypeError, OSError):
            draw.text((x, y), run, font=run_font, fill=fill)
        x += draw.textlength(run, font=run_font)


def _draw_centered_text(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont, fill: Any) -> None:
    _draw_text(draw, ((CANVAS_WIDTH - _text_length(text, font)) / 2, y), text, font, fill)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for source_line in text.splitlines() or [""]:
        current = ""
        for character in source_line:
            candidate = current + character
            if current and _text_length(candidate, font) > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _fit_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return max_width, min(max_height, 100)
    scale = min(max_width / width, max_height / height, 1)
    return max(1, int(width * scale)), max(1, int(height * scale))


def _fit_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if _text_length(text, font) <= max_width:
        return text
    suffix = "..."
    current = ""
    for character in text:
        if _text_length(current + character + suffix, font) > max_width:
            break
        current += character
    return current + suffix


def _at_display_name(data: dict[str, Any]) -> str:
    for key in ("display_name", "name", "text"):
        value = str(data.get(key) or "").strip()
        if value.startswith("@"):
            value = value[1:].strip()
        if value and not value.isdigit():
            return value
    return ""


def _media_text(data: Any, key: str) -> str:
    if isinstance(data, dict):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""
