from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_TEMPLATE = "来自 {source_group_id} 的新消息：\n发送者：{sender_nickname}（{sender_id}）\n发送时间：{time}\n{message}"
ALLOWED_TEMPLATE_FIELDS = {"sender_nickname", "sender_id", "time", "message", "source_group_id"}


@dataclass(frozen=True, slots=True)
class ForwardGroupConfig:
    id: str
    name: str
    source_group_id: int
    target_group_ids: tuple[int, ...]
    forward_template: str = DEFAULT_TEMPLATE
    batch_size: int = 1
    watched_qq_ids: tuple[str, ...] = ()
    qz_poll_interval: int = 30
    qz_action: str = "get_emotion_list"
    forward_mode: str = "image"

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        index: int = 0,
        default_template: str = DEFAULT_TEMPLATE,
    ) -> "ForwardGroupConfig":
        target_values = raw.get("target_group_ids", raw.get("target_group_id", []))
        if not isinstance(target_values, (list, tuple, set)):
            target_values = [target_values]
        target_ids = tuple(
            dict.fromkeys(
                _to_group_id(value, "target_group_id")
                for value in target_values
                if value not in (None, "")
            )
        )

        watched_values = raw.get("watched_qq_ids", raw.get("qz_qq_ids", []))
        if not isinstance(watched_values, (list, tuple, set)):
            watched_values = [watched_values]
        watched_ids = tuple(
            dict.fromkeys(str(value).strip() for value in watched_values if str(value).strip())
        )

        cfg = cls(
            id=_group_key(raw.get("id") or raw.get("name") or f"group-{index + 1}"),
            name=str(raw.get("name") or f"分组 {index + 1}").strip() or f"分组 {index + 1}",
            source_group_id=_to_group_id(raw.get("source_group_id", 0), "source_group_id"),
            target_group_ids=target_ids,
            forward_template=str(raw.get("forward_template", default_template)),
            batch_size=_to_batch_size(raw.get("batch_size", 1)),
            watched_qq_ids=watched_ids,
            qz_poll_interval=_to_qz_interval(raw.get("qz_poll_interval", 30)),
            qz_action=str(raw.get("qz_action", "get_emotion_list")).strip() or "get_emotion_list",
            forward_mode=str(raw.get("forward_mode", "image")).strip().lower(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.forward_mode not in {"image", "forward"}:
            raise ValueError("forward_mode 必须是 image 或 forward")
        if self.source_group_id and self.source_group_id in self.target_group_ids:
            raise ValueError(f"分组 {self.name} 的源群和目标群不能相同")
        if not self.forward_template:
            raise ValueError(f"分组 {self.name} 的转发模板不能为空")
        formatter = Formatter()
        for _, field_name, _, _ in formatter.parse(self.forward_template):
            if field_name is None:
                continue
            if field_name not in ALLOWED_TEMPLATE_FIELDS:
                allowed = ", ".join(sorted(ALLOWED_TEMPLATE_FIELDS))
                raise ValueError(f"分组 {self.name} 的模板包含不支持的占位符 {{{field_name}}}；仅支持: {allowed}")
        for user_id in self.watched_qq_ids:
            if not user_id.isdigit():
                raise ValueError(f"分组 {self.name} 的监听 QQ 号必须是数字: {user_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_group_id": self.source_group_id,
            "target_group_ids": list(self.target_group_ids),
            "forward_template": self.forward_template,
            "batch_size": self.batch_size,
            "watched_qq_ids": list(self.watched_qq_ids),
            "qz_poll_interval": self.qz_poll_interval,
            "qz_action": self.qz_action,
            "forward_mode": self.forward_mode,
        }


@dataclass(frozen=True, slots=True)
class AppConfig:
    snowluma_ws_url: str = "ws://127.0.0.1:8095"
    web_port: int = 8000
    groups: tuple[ForwardGroupConfig, ...] = ()
    backend: str = "auto"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        defaults = cls()
        group_values = raw.get("groups")
        if isinstance(group_values, list):
            groups = tuple(
                ForwardGroupConfig.from_dict(
                    item,
                    index,
                    str(raw.get("forward_template", DEFAULT_TEMPLATE)),
                )
                for index, item in enumerate(group_values)
                if isinstance(item, dict)
            )
        else:
            target_group_id = raw.get("target_group_id", 0)
            groups = (
                ForwardGroupConfig.from_dict(
                    {
                        "id": "default",
                        "name": "默认分组",
                        "source_group_id": raw.get("source_group_id", 0),
                        "target_group_ids": [target_group_id]
                        if target_group_id not in (None, "", 0)
                        else [],
                        "forward_template": raw.get("forward_template", DEFAULT_TEMPLATE),
                        "batch_size": raw.get("batch_size", 1),
                        "forward_mode": raw.get("forward_mode", "image"),
                        "watched_qq_ids": raw.get("watched_qq_ids", []),
                        "qz_poll_interval": raw.get("qz_poll_interval", 30),
                        "qz_action": raw.get("qz_action", "get_emotion_list"),
                    },
                    0,
                    str(raw.get("forward_template", DEFAULT_TEMPLATE)),
                ),
            )

        cfg = cls(
            snowluma_ws_url=str(raw.get("snowluma_ws_url", defaults.snowluma_ws_url)).strip(),
            web_port=_to_port(raw.get("web_port", defaults.web_port)),
            groups=groups,
            backend=str(raw.get("backend", defaults.backend)).strip().lower(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.backend not in {"auto", "go-cqhttp", "napcat"}:
            raise ValueError("backend 必须是 auto、go-cqhttp 或 napcat")
        if not self.snowluma_ws_url.startswith(("ws://", "wss://")):
            raise ValueError("snowluma_ws_url 必须以 ws:// 或 wss:// 开头")
        seen_ids: set[str] = set()
        source_ids: set[int] = set()
        target_ids: set[int] = set()
        for group in self.groups:
            group.validate()
            if group.id in seen_ids:
                raise ValueError(f"分组 ID 重复: {group.id}")
            seen_ids.add(group.id)
            if group.source_group_id:
                if group.source_group_id in source_ids:
                    raise ValueError(f"源群不能重复配置: {group.source_group_id}")
                source_ids.add(group.source_group_id)
            for target_id in group.target_group_ids:
                if target_id in target_ids:
                    raise ValueError(f"目标群不能属于多个分组: {target_id}")
                target_ids.add(target_id)
        overlap = source_ids & target_ids
        if overlap:
            raise ValueError(
                f"群号不能同时作为源群和目标群: {', '.join(str(value) for value in sorted(overlap))}"
            )

    def to_dict(self) -> dict[str, Any]:
        first = self.groups[0] if self.groups else None
        data: dict[str, Any] = {
            "snowluma_ws_url": self.snowluma_ws_url,
            "backend": self.backend,
            "web_port": self.web_port,
            "groups": [group.to_dict() for group in self.groups],
        }
        if first is not None:
            data.update(
                {
                    "source_group_id": first.source_group_id,
                    "target_group_id": first.target_group_ids[0]
                    if first.target_group_ids
                    else 0,
                    "forward_template": first.forward_template,
                    "batch_size": first.batch_size,
                    "forward_mode": first.forward_mode,
                }
            )
        return data

    @property
    def source_group_id(self) -> int:
        return self.groups[0].source_group_id if self.groups else 0

    @property
    def target_group_id(self) -> int:
        return self.groups[0].target_group_ids[0] if self.groups and self.groups[0].target_group_ids else 0


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._config = self._load_or_create()

    def get(self) -> AppConfig:
        with self._lock:
            return self._config

    def update(self, patch: dict[str, Any]) -> tuple[AppConfig, AppConfig]:
        allowed = {
            "backend",
            "forward_mode",
            "snowluma_ws_url",
            "web_port",
            "groups",
            "source_group_id",
            "target_group_id",
            "forward_template",
            "batch_size",
            "watched_qq_ids",
            "qz_poll_interval",
            "qz_action",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")

        with self._lock:
            old = self._config
            if "groups" in patch:
                merged = {
                    "backend": patch.get("backend", old.backend),
                    "snowluma_ws_url": patch.get("snowluma_ws_url", old.snowluma_ws_url),
                    "web_port": patch.get("web_port", old.web_port),
                    "groups": patch["groups"],
                }
            else:
                merged = old.to_dict()
                merged.update({key: value for key, value in patch.items() if value is not None})
                legacy_fields = {
                    "forward_mode",
                    "source_group_id",
                    "target_group_id",
                    "forward_template",
                    "batch_size",
                    "watched_qq_ids",
                    "qz_poll_interval",
                    "qz_action",
                }
                if legacy_fields & set(patch):
                    first = dict(merged["groups"][0]) if merged.get("groups") else {
                        "id": "default",
                        "name": "默认分组",
                        "source_group_id": 0,
                        "target_group_ids": [],
                    }
                    if "source_group_id" in patch:
                        first["source_group_id"] = patch["source_group_id"]
                    if "target_group_id" in patch:
                        first["target_group_ids"] = [patch["target_group_id"]]
                    for key in ("forward_template", "forward_mode", "batch_size", "watched_qq_ids", "qz_poll_interval", "qz_action"):
                        if key in patch:
                            first[key] = patch[key]
                    merged["groups"] = [first] + list(merged.get("groups", [])[1:])
            new = AppConfig.from_dict(merged)
            self._save(new)
            self._config = new
            return old, new

    def _load_or_create(self) -> AppConfig:
        if not self.path.exists():
            cfg = AppConfig.from_dict({})
            self._save(cfg)
            return cfg

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"config.json 不是有效 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("config.json 顶层必须是 JSON 对象")
        cfg = AppConfig.from_dict(raw)
        if "groups" not in raw:
            self._save(cfg)
        return cfg

    def _save(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


def _group_key(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return "group"
    return "".join(character if character.isalnum() or character in "-_" else "-" for character in text)


def _to_group_id(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数群号") from exc
    if result < 0:
        raise ValueError(f"{name} 不能小于 0")
    return result


def _to_batch_size(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch_size 必须是整数") from exc
    if not 1 <= result <= 50:
        raise ValueError("batch_size 必须在 1~50 之间")
    return result


def _to_qz_interval(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("qz_poll_interval 必须是整数") from exc
    if not 5 <= result <= 3600:
        raise ValueError("qz_poll_interval 必须在 5~3600 秒之间")
    return result


def _to_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("web_port 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("web_port 必须在 1~65535 之间")
    return port
