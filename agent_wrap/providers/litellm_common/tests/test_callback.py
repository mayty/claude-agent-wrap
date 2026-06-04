# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/callback.py."""

from __future__ import annotations

import json

from agent_wrap.providers.litellm_common.callback import build_record


def test_build_record_success_shape() -> None:
    kwargs = {
        "model": "bedrock/claude",
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_params": {"proxy_server_request": {"url": "/bedrock/x", "body": {"a": 1}}},
    }
    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    assert record["status"] == "success"
    assert record["model"] == "bedrock/claude"
    assert record["request"]["messages"] == kwargs["messages"]
    assert record["request"]["proxy_server_request"]["url"] == "/bedrock/x"
    assert record["response"] == {"choices": [{"text": "yo"}]}
    assert "error" not in record
    assert "ts" in record


def test_build_record_failure_includes_error() -> None:
    record = build_record({}, None, status="failure", exc=RuntimeError("boom"))
    assert record["status"] == "failure"
    assert record["error"] == "boom"


def test_build_record_tolerates_missing_keys() -> None:
    record = build_record({}, None, status="success")
    assert record["model"] is None
    assert record["request"]["messages"] is None
    assert record["request"]["proxy_server_request"] is None


def test_build_record_is_json_serializable_with_default_str() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-obj"

    record = build_record({"model": "m", "messages": [Weird()]}, Weird(), status="success")
    # default=str mirrors how the callback writes the line.
    line = json.dumps(record, default=str)
    assert "weird-obj" in line


def test_build_record_breaks_circular_references() -> None:
    # LiteLLM's response objects contain cycles; build_record must not raise.
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic

    record = build_record({"model": "m"}, cyclic, status="success")
    line = json.dumps(record)  # no default= needed; build_record made it safe

    assert "<circular>" in line
    assert record["response"]["a"] == 1


def test_build_record_uses_model_dump_for_pydantic_like() -> None:
    class Modelish:
        def model_dump(self) -> dict[str, object]:
            return {"kind": "modelish", "n": 1}

    record = build_record({"model": "m"}, Modelish(), status="success")
    assert record["response"] == {"kind": "modelish", "n": 1}
