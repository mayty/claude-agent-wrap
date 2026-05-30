# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.providers."""

import inspect
import unittest

from agent_wrap.providers import _discover_providers, get_provider
from agent_wrap.providers.base import Provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


class TestProviderDiscovery(unittest.TestCase):
    def test_discovers_bedrock(self):
        registry = _discover_providers()
        self.assertIn("litellm-bedrock", registry)

    def test_discovers_dashscope(self):
        registry = _discover_providers()
        self.assertIn("litellm-dashscope", registry)

    def test_excludes_abstract_classes(self):
        registry = _discover_providers()
        for name, cls in registry.items():
            self.assertFalse(inspect.isabstract(cls), f"{name} is abstract")

    def test_exactly_two_providers(self):
        registry = _discover_providers()
        self.assertEqual(len(registry), 2)


class TestGetProvider(unittest.TestCase):
    def test_default_is_bedrock(self):
        p = get_provider()
        self.assertEqual(p.name, "litellm-bedrock")

    def test_explicit_bedrock(self):
        p = get_provider("litellm-bedrock")
        self.assertEqual(p.name, "litellm-bedrock")

    def test_explicit_dashscope(self):
        p = get_provider("litellm-dashscope")
        self.assertEqual(p.name, "litellm-dashscope")

    def test_unknown_provider_exits(self):
        with self.assertRaises(SystemExit):
            get_provider("nonexistent-provider")


class TestProviderInterface(unittest.TestCase):
    def test_bedrock_implements_all_methods(self):
        p = get_provider("litellm-bedrock")
        self.assertTrue(hasattr(p, "ensure"))
        self.assertTrue(hasattr(p, "release"))
        self.assertTrue(hasattr(p, "get_run_args"))
        self.assertTrue(hasattr(p, "get_label_args"))

    def test_provider_is_abstract(self):
        self.assertTrue(inspect.isabstract(Provider))

    def test_litellm_provider_is_abstract(self):
        self.assertTrue(inspect.isabstract(LiteLLMProvider))


class TestProviderAttributes(unittest.TestCase):
    def test_bedrock_lock_file(self):
        p = get_provider("litellm-bedrock")
        self.assertEqual(p.lock_file, "litellm.lock")

    def test_dashscope_lock_file(self):
        p = get_provider("litellm-dashscope")
        self.assertEqual(p.lock_file, "litellm-dashscope.lock")

    def test_bedrock_master_key_prefix(self):
        p = get_provider("litellm-bedrock")
        self.assertEqual(p.master_key_prefix, "sk-aw-")

    def test_dashscope_master_key_prefix(self):
        p = get_provider("litellm-dashscope")
        self.assertEqual(p.master_key_prefix, "sk-ds-")


class TestLabelArgs(unittest.TestCase):
    def test_bedrock_label_args(self):
        p = get_provider("litellm-bedrock")
        args = p.get_label_args("test-123")
        self.assertIn("--label", args)
        self.assertIn("agent-wrap.role=claude-agent", args)
        self.assertIn("--name", args)
        self.assertIn("claude-agent-test-123", args)

    def test_empty_instance_id(self):
        p = get_provider("litellm-bedrock")
        self.assertEqual(p.get_label_args(""), [])


class TestGetRunArgs(unittest.TestCase):
    def test_returns_list(self):
        p = get_provider("litellm-bedrock")
        self.assertIsInstance(p.get_run_args(), list)

    def test_empty_before_ensure(self):
        p = get_provider("litellm-bedrock")
        self.assertEqual(p.get_run_args(), [])
