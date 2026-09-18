# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.config.project_registry."""

import pytest

from agent_wrap.domain.config.project_registry import ProjectRegistry


def test_decompress_empty() -> None:
    assert ProjectRegistry.decompress([]) == []


def test_decompress_plain() -> None:
    assert ProjectRegistry.decompress(["/a/b", "/c/d"]) == ["/a/b", "/c/d"]


def test_decompress_sibling() -> None:
    assert ProjectRegistry.decompress(["/a/{x,y}"]) == ["/a/x", "/a/y"]


def test_decompress_prefix() -> None:
    assert ProjectRegistry.decompress(["/home/user/foo", "{2}/bar"]) == [
        "/home/user/foo",
        "/home/user/bar",
    ]


def test_decompress_combined() -> None:
    assert ProjectRegistry.decompress(["/a/b/c", "{2}/{d,e}"]) == [
        "/a/b/c",
        "/a/b/d",
        "/a/b/e",
    ]


def test_decompress_spec_example() -> None:
    compressed = [
        "/home/p_pikirenya/GSR/playground/wtrcal",
        "{3}/{wgsh,wotp,wotp-be,wtrcal}",
        "{4}/playground/new_2026",
    ]
    assert ProjectRegistry.decompress(compressed) == [
        "/home/p_pikirenya/GSR/playground/wtrcal",
        "/home/p_pikirenya/GSR/wgsh",
        "/home/p_pikirenya/GSR/wotp",
        "/home/p_pikirenya/GSR/wotp-be",
        "/home/p_pikirenya/GSR/wtrcal",
        "/home/p_pikirenya/GSR/wtrcal/playground/new_2026",
    ]


def test_decompress_ignores_literal_braces() -> None:
    assert ProjectRegistry.decompress(["/a/{braces}/c"]) == ["/a/{braces}/c"]


def test_decompress_skips_empty_lines() -> None:
    assert ProjectRegistry.decompress(["/a", "", "/b"]) == ["/a", "/b"]


def test_decompress_first_line_prefix_is_skipped() -> None:
    assert ProjectRegistry.decompress(["{3}/foo"]) == []


# Every shape the writer that produced these files could emit, paired with the paths it
# stood for. These were the round-trip cases checked against the encoder; the encoder is
# gone (nothing has written this format since the SQLite registry landed), so the wire
# shapes are pinned here directly.
_ENCODED_SHAPES: list[tuple[list[str], list[str]]] = [
    (["/{a,b}"], ["/a", "/b"]),
    (["/a/{x,z}", "/b/y"], ["/a/x", "/a/z", "/b/y"]),
    (["/a/b/c", "{3}/d"], ["/a/b/c", "/a/b/c/d"]),
    (
        ["/home/user/bar/beta", "{2}/foo/alpha"],
        ["/home/user/bar/beta", "/home/user/foo/alpha"],
    ),
    (
        ["/home/user/other/{x,y,z}", "{2}/proj/{a,b}"],
        [
            "/home/user/other/x",
            "/home/user/other/y",
            "/home/user/other/z",
            "/home/user/proj/a",
            "/home/user/proj/b",
        ],
    ),
]


@pytest.mark.parametrize(("encoded", "expected"), _ENCODED_SHAPES)
def test_decompress_encoded_shape(encoded: list[str], expected: list[str]) -> None:
    assert ProjectRegistry.decompress(encoded) == expected
