# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.config.project_registry."""

from __future__ import annotations

import pytest

from agent_wrap.domain.config.project_registry import ProjectRegistry

# ------------------------------------------------------------------
# compress
# ------------------------------------------------------------------


def test_compress_empty() -> None:
    assert ProjectRegistry.compress([]) == []


def test_compress_single() -> None:
    assert ProjectRegistry.compress(["/a/b"]) == ["/a/b"]


def test_compress_no_common_prefix() -> None:
    assert ProjectRegistry.compress(["/a/b", "/c/d"]) == ["/a/b", "/c/d"]


def test_compress_sibling_pair() -> None:
    assert ProjectRegistry.compress(["/a/x", "/a/y"]) == ["/a/{x,y}"]


def test_compress_sibling_many() -> None:
    assert ProjectRegistry.compress(["/a/x", "/a/y", "/a/z"]) == ["/a/{x,y,z}"]


def test_compress_sibling_non_consecutive() -> None:
    assert ProjectRegistry.compress(["/a/x", "/b/y", "/a/z"]) == [
        "/a/{x,z}",
        "/b/y",
    ]


def test_compress_prefix_shared() -> None:
    """Paths with different parents get prefix-shared, not sibling-grouped."""
    assert ProjectRegistry.compress(["/home/user/foo/alpha", "/home/user/bar/beta"]) == [
        "/home/user/bar/beta",
        "{2}/foo/alpha",
    ]


def test_compress_prefix_no_shared() -> None:
    assert ProjectRegistry.compress(["/home/user/foo", "/other/path"]) == [
        "/home/user/foo",
        "/other/path",
    ]


def test_compress_combined_from_spec() -> None:
    """The combined example from the design spec."""
    paths = [
        "/home/p_pikirenya/GSR/playground/wtrcal",
        "/home/p_pikirenya/GSR/wgsh",
        "/home/p_pikirenya/GSR/wotp",
        "/home/p_pikirenya/GSR/wotp-be",
        "/home/p_pikirenya/GSR/wtrcal",
        "/home/p_pikirenya/GSR/wtrcal/playground/new_2026",
    ]
    assert ProjectRegistry.compress(paths) == [
        "/home/p_pikirenya/GSR/playground/wtrcal",
        "{3}/{wgsh,wotp,wotp-be,wtrcal}",
        "{4}/playground/new_2026",
    ]


def test_compress_path_is_prefix_of_another() -> None:
    assert ProjectRegistry.compress(["/a/b/c", "/a/b/c/d"]) == [
        "/a/b/c",
        "{3}/d",
    ]


def test_compress_root_siblings() -> None:
    assert ProjectRegistry.compress(["/a", "/b"]) == ["/{a,b}"]


def test_compress_mixed_siblings_and_singletons() -> None:
    paths = [
        "/home/user/proj/a",
        "/home/user/proj/b",
        "/home/user/other/x",
        "/home/user/other/y",
        "/home/user/other/z",
    ]
    result = ProjectRegistry.compress(paths)
    assert result == [
        "/home/user/other/{x,y,z}",
        "{2}/proj/{a,b}",
    ]


# ------------------------------------------------------------------
# decompress
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# roundtrip
# ------------------------------------------------------------------

_ROUNDTRIP_CASES: list[list[str]] = [
    [],
    ["/a"],
    ["/a/b", "/a/c"],
    ["/home/user/proj", "/home/user/other"],
    ["/a/x", "/a/y", "/a/z"],
    ["/a/b/c", "/a/b/d", "/a/b/e"],
    ["/x/y/z/a", "/x/y/z/b", "/x/y/w/c"],
    [
        "/home/p_pikirenya/GSR/playground/wtrcal",
        "/home/p_pikirenya/GSR/wgsh",
        "/home/p_pikirenya/GSR/wotp",
        "/home/p_pikirenya/GSR/wotp-be",
        "/home/p_pikirenya/GSR/wtrcal",
        "/home/p_pikirenya/GSR/wtrcal/playground/new_2026",
    ],
]


@pytest.mark.parametrize("paths", _ROUNDTRIP_CASES)
def test_roundtrip(paths: list[str]) -> None:
    assert ProjectRegistry.decompress(ProjectRegistry.compress(paths)) == sorted(set(paths))
