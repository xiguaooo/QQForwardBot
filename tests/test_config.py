import json
import tempfile
import unittest
from pathlib import Path

from config import AppConfig, ConfigManager, ForwardGroupConfig


class BackendConfigTests(unittest.TestCase):
    def test_old_config_retains_image_mode(self):
        cfg = AppConfig.from_dict({"source_group_id": 123, "target_group_id": 456})
        self.assertEqual(cfg.backend, "auto")
        self.assertEqual(cfg.groups[0].forward_mode, "image")

    def test_explicit_backend_and_mode_round_trip(self):
        cfg = AppConfig.from_dict({
            "backend": "go-cqhttp", "snowluma_ws_url": "ws://localhost:8095/",
            "groups": [{"source_group_id": 123, "target_group_ids": [456], "forward_mode": "forward"}],
        })
        self.assertEqual(AppConfig.from_dict(cfg.to_dict()), cfg)
        self.assertEqual(cfg.groups[0].forward_mode, "forward")

    def test_rejects_unknown_backend_and_mode(self):
        with self.assertRaisesRegex(ValueError, "backend"):
            AppConfig.from_dict({"backend": "unknown"})
        with self.assertRaisesRegex(ValueError, "forward_mode"):
            ForwardGroupConfig.from_dict({"forward_mode": "unknown"})

    def test_group_update_preserves_selected_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            manager = ConfigManager(path)
            manager.update({"backend": "go-cqhttp"})
            _, cfg = manager.update({"groups": [{"forward_mode": "forward"}]})
            self.assertEqual(cfg.backend, "go-cqhttp")
            self.assertEqual(ConfigManager(path).get(), cfg)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["backend"], "go-cqhttp")

    def test_legacy_mode_patch_updates_first_route(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ConfigManager(Path(directory) / "config.json")
            _, cfg = manager.update({"backend": "napcat", "forward_mode": "forward"})
            self.assertEqual(cfg.backend, "napcat")
            self.assertEqual(cfg.groups[0].forward_mode, "forward")


if __name__ == "__main__":
    unittest.main()
