# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.utils."""

import tempfile
import unittest
from pathlib import Path

from agent_wrap.utils import (
    generate_uuid,
    parse_dockerfile_agent,
    sanitize_name,
)


class TestSanitizeName(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(sanitize_name("Hello"), "hello")

    def test_replace_spaces(self):
        self.assertEqual(sanitize_name("hello world"), "hello-world")

    def test_replace_special_chars(self):
        self.assertEqual(sanitize_name("hello@world!"), "hello-world")

    def test_strip_leading_trailing_dashes(self):
        self.assertEqual(sanitize_name("---hello---"), "hello")

    def test_preserve_dots_underscores_dashes(self):
        self.assertEqual(sanitize_name("my-project_v2.0"), "my-project_v2.0")

    def test_empty_after_sanitize(self):
        self.assertEqual(sanitize_name("---"), "")

    def test_mixed_case_and_special(self):
        self.assertEqual(sanitize_name("My Project (v2)"), "my-project--v2")


class TestGenerateUuid(unittest.TestCase):
    def test_returns_string(self):
        self.assertIsInstance(generate_uuid(), str)

    def test_format(self):
        parts = generate_uuid().split("-")
        self.assertEqual(len(parts), 5)

    def test_lowercase(self):
        result = generate_uuid()
        self.assertEqual(result, result.lower())

    def test_unique(self):
        self.assertNotEqual(generate_uuid(), generate_uuid())


class TestParseDockerfileAgent(unittest.TestCase):
    def _write_temp(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".agent", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_agent_user(self):
        p = self._write_temp("# agent-name: test\n# agent-user: customuser\nFROM claude-agent\n")
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.agent_user, "customuser")

    def test_default_agent_user(self):
        p = self._write_temp("# agent-name: test\nFROM claude-agent\n")
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.agent_user, "ubuntu")

    def test_expose_ports(self):
        p = self._write_temp("FROM claude-agent\nEXPOSE 8080 3000/tcp\n")
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.expose_ports, ["8080", "3000"])

    def test_agent_run_args(self):
        p = self._write_temp("FROM claude-agent\n# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN\n")
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.extra_run_args, ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"])

    def test_multiple_run_args_lines(self):
        p = self._write_temp(
            "FROM claude-agent\n"
            "# agent-run-args: --device /dev/fuse\n"
            "# agent-run-args: --cap-add SYS_ADMIN\n"
        )
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.extra_run_args, ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"])

    def test_empty_dockerfile(self):
        p = self._write_temp("FROM claude-agent\n")
        info = parse_dockerfile_agent(p)
        self.assertEqual(info.agent_user, "ubuntu")
        self.assertEqual(info.expose_ports, [])
        self.assertEqual(info.extra_run_args, [])
