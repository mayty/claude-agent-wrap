# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.commands."""

import unittest

from agent_wrap.commands.agent import _extract_network, _is_truthy


class TestExtractNetwork(unittest.TestCase):
    def test_no_network(self):
        self.assertIsNone(_extract_network([]))
        self.assertIsNone(_extract_network(["--device", "/dev/fuse"]))

    def test_separate_flag(self):
        self.assertEqual(_extract_network(["--network", "mynet"]), "mynet")

    def test_equals_syntax(self):
        self.assertEqual(_extract_network(["--network=mynet"]), "mynet")

    def test_net_alias(self):
        self.assertEqual(_extract_network(["--net", "mynet"]), "mynet")
        self.assertEqual(_extract_network(["--net=mynet"]), "mynet")

    def test_first_occurrence_wins(self):
        self.assertEqual(
            _extract_network(["--network", "first", "--network", "second"]),
            "first",
        )

    def test_missing_value(self):
        self.assertIsNone(_extract_network(["--network"]))

    def test_among_other_flags(self):
        args = ["--device", "/dev/fuse", "--network", "mynet", "--cap-add", "SYS_ADMIN"]
        self.assertEqual(_extract_network(args), "mynet")


class TestIsTruthy(unittest.TestCase):
    def test_empty_is_false(self):
        self.assertFalse(_is_truthy(""))

    def test_zero_is_false(self):
        self.assertFalse(_is_truthy("0"))

    def test_false_is_false(self):
        self.assertFalse(_is_truthy("false"))
        self.assertFalse(_is_truthy("FALSE"))

    def test_no_is_false(self):
        self.assertFalse(_is_truthy("no"))
        self.assertFalse(_is_truthy("NO"))

    def test_one_is_true(self):
        self.assertTrue(_is_truthy("1"))

    def test_yes_is_true(self):
        self.assertTrue(_is_truthy("yes"))

    def test_any_string_is_true(self):
        self.assertTrue(_is_truthy("hello"))
