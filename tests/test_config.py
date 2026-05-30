# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.config."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_wrap.config import ensure_statusline, ensure_telegram_hooks


class TestEnsureStatusline(unittest.TestCase):
    def test_injects_into_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_statusline(settings)
            data = json.loads(settings.read_text())
            self.assertIn("statusLine", data)
            self.assertEqual(data["statusLine"]["type"], "command")
            self.assertIn("statusline.py", data["statusLine"]["command"])

    def test_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            ensure_statusline(settings)
            data = json.loads(settings.read_text())
            self.assertIn("statusLine", data)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_statusline(settings)
            first = json.loads(settings.read_text())
            ensure_statusline(settings)
            second = json.loads(settings.read_text())
            self.assertEqual(first, second)

    def test_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            custom = {"statusLine": {"type": "command", "command": "/custom/script"}}
            settings.write_text(json.dumps(custom))
            ensure_statusline(settings)
            data = json.loads(settings.read_text())
            self.assertEqual(data["statusLine"]["command"], "/custom/script")

    def test_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"theme": "dark"}))
            ensure_statusline(settings)
            data = json.loads(settings.read_text())
            self.assertEqual(data["theme"], "dark")
            self.assertIn("statusLine", data)

    def test_skips_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{bad json")
            ensure_statusline(settings)
            self.assertEqual(settings.read_text(), "{bad json")


class TestEnsureTelegramHooks(unittest.TestCase):
    def test_injects_all_three_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_telegram_hooks(settings)
            data = json.loads(settings.read_text())
            hooks = data["hooks"]
            self.assertIn("PermissionRequest", hooks)
            self.assertIn("Stop", hooks)
            self.assertIn("StopFailure", hooks)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_telegram_hooks(settings)
            first = json.loads(settings.read_text())
            ensure_telegram_hooks(settings)
            second = json.loads(settings.read_text())
            self.assertEqual(first, second)

    def test_stop_has_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_telegram_hooks(settings)
            data = json.loads(settings.read_text())
            cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertTrue(cmd.endswith("stop"))

    def test_stopfailure_has_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}")
            ensure_telegram_hooks(settings)
            data = json.loads(settings.read_text())
            cmd = data["hooks"]["StopFailure"][0]["hooks"][0]["command"]
            self.assertTrue(cmd.endswith("stopfailure"))

    def test_preserves_existing_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            existing = {"hooks": {"PreToolUse": [{"matcher": "", "hooks": []}]}}
            settings.write_text(json.dumps(existing))
            ensure_telegram_hooks(settings)
            data = json.loads(settings.read_text())
            self.assertIn("PreToolUse", data["hooks"])
            self.assertIn("PermissionRequest", data["hooks"])
