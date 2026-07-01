# This file has been created with the assistance of an AI tool.
"""Tests for the usage-stats range grammar (--from/--until/--days resolution)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import agent_wrap.lib.usage_args as ua
from agent_wrap.lib.format import day_in_range
from agent_wrap.lib.usage_args import DEFAULT_DAYS, parse_usage_args

if TYPE_CHECKING:
    from pathlib import Path

# A fixed "today" so relative offsets and defaults are deterministic.
_TODAY = date(2026, 6, 29)


def _freeze_today(mocker):
    # _today() returns an aware datetime whose local date is _TODAY; a noon naive
    # datetime made aware via astimezone keeps the calendar day in any local tz.
    frozen = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, 0, 0).astimezone()
    mocker.patch.object(ua, "_today", return_value=frozen)


def _parse(mocker, reg: Path, *flags: str):
    _freeze_today(mocker)
    return parse_usage_args([str(reg), *flags], usage_line="u", usage_text="u")


def _reg(tmp_path: Path) -> Path:
    reg = tmp_path / "projects.txt"
    reg.write_text("/x\n", encoding="utf-8")
    return reg


def _iso(d: date) -> str:
    return d.isoformat()


# --- resolution table --------------------------------------------------------


def test_no_flags_defaults_to_last_28_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path))
    assert parsed is not None
    assert parsed.from_iso == _iso(_TODAY - timedelta(days=DEFAULT_DAYS - 1))
    assert parsed.until_iso == _iso(_TODAY)
    assert parsed.verbose is False


def test_from_alone_runs_to_today(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "2026-06-01")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso == _iso(_TODAY)


def test_until_alone_spans_default_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--until", "2026-06-20")
    assert parsed is not None
    assert parsed.until_iso == "2026-06-20"
    assert parsed.from_iso == _iso(date(2026, 6, 20) - timedelta(days=DEFAULT_DAYS - 1))


def test_days_alone(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--days", "7")
    assert parsed is not None
    assert parsed.from_iso == _iso(_TODAY - timedelta(days=7 - 1))
    assert parsed.until_iso == _iso(_TODAY)


def test_days_zero_is_all_time(mocker, tmp_path: Path):
    # --days 0 lifts the count bound: open lower side, but the implicit upper stays
    # "now" (no --until given). Records carry timestamps <= now, so this is all-time.
    parsed = _parse(mocker, _reg(tmp_path), "--days", "0")
    assert parsed is not None
    assert parsed.from_iso is None
    assert parsed.until_iso == _iso(_TODAY)


def test_from_and_until(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "2026-06-01", "--until", "2026-06-10")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso == "2026-06-10"


def test_from_and_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "2026-06-01", "--days", "5")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso == "2026-06-05"


def test_from_and_days_zero_open_upper(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "2026-06-01", "--days", "0")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso is None


def test_until_and_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--until", "2026-06-20", "--days", "5")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-16"
    assert parsed.until_iso == "2026-06-20"


def test_until_and_days_zero_open_lower(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--until", "2026-06-20", "--days", "0")
    assert parsed is not None
    assert parsed.from_iso is None
    assert parsed.until_iso == "2026-06-20"


# --- short flag forms --------------------------------------------------------


def test_short_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "-d", "7")
    assert parsed is not None
    assert parsed.from_iso == _iso(_TODAY - timedelta(days=7 - 1))
    assert parsed.until_iso == _iso(_TODAY)


def test_short_from(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "-f", "2026-06-01")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso == _iso(_TODAY)


def test_short_until(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "-u", "2026-06-20")
    assert parsed is not None
    assert parsed.until_iso == "2026-06-20"
    assert parsed.from_iso == _iso(date(2026, 6, 20) - timedelta(days=DEFAULT_DAYS - 1))


def test_short_from_and_until(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "-f", "2026-06-01", "-u", "2026-06-10")
    assert parsed is not None
    assert parsed.from_iso == "2026-06-01"
    assert parsed.until_iso == "2026-06-10"


# --- relative date specs -----------------------------------------------------


def test_relative_from(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "-14d")
    assert parsed is not None
    assert parsed.from_iso == _iso(_TODAY - timedelta(days=14))
    assert parsed.until_iso == _iso(_TODAY)


def test_relative_until_and_days(mocker, tmp_path: Path):
    parsed = _parse(mocker, _reg(tmp_path), "--until", "-7d", "--days", "3")
    assert parsed is not None
    assert parsed.until_iso == _iso(_TODAY - timedelta(days=7))
    assert parsed.from_iso == _iso(_TODAY - timedelta(days=9))


# --- errors ------------------------------------------------------------------


def test_all_three_flags_rejected(mocker, tmp_path: Path, capsys):
    parsed = _parse(
        mocker, _reg(tmp_path), "--from", "2026-06-01", "--until", "2026-06-10", "--days", "3"
    )
    assert parsed is None
    assert "at most two" in capsys.readouterr().err


def test_from_after_until_rejected(mocker, tmp_path: Path, capsys):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "2026-06-10", "--until", "2026-06-01")
    assert parsed is None
    assert "after --until" in capsys.readouterr().err


def test_malformed_from_rejected(mocker, tmp_path: Path, capsys):
    parsed = _parse(mocker, _reg(tmp_path), "--from", "june-first")
    assert parsed is None
    err = capsys.readouterr().err
    assert "-f/--from" in err
    assert "expects" in err


def test_negative_days_rejected(mocker, tmp_path: Path, capsys):
    parsed = _parse(mocker, _reg(tmp_path), "--days", "-3")
    assert parsed is None
    assert "must be >= 0" in capsys.readouterr().err


def test_days_missing_value_rejected(mocker, tmp_path: Path, capsys):
    # Previously the bare flag was silently treated as the positional registry
    # path; argparse now reports the missing value instead.
    parsed = _parse(mocker, _reg(tmp_path), "--days")
    assert parsed is None
    assert "argument" in capsys.readouterr().err


def test_missing_registry_rejected(mocker, capsys):
    _freeze_today(mocker)
    parsed = parse_usage_args([], usage_line="u", usage_text="u")
    assert parsed is None
    assert capsys.readouterr().err


# --- day_in_range ------------------------------------------------------------


def test_day_in_range_inclusive_bounds():
    assert day_in_range("2026-06-01", "2026-06-01", "2026-06-10") is True
    assert day_in_range("2026-06-10", "2026-06-01", "2026-06-10") is True
    assert day_in_range("2026-05-31", "2026-06-01", "2026-06-10") is False
    assert day_in_range("2026-06-11", "2026-06-01", "2026-06-10") is False


def test_day_in_range_open_sides():
    assert day_in_range("2000-01-01", None, "2026-06-10") is True
    assert day_in_range("2030-01-01", "2026-06-01", None) is True
    assert day_in_range("2026-06-05", None, None) is True


def test_day_in_range_question_mark_only_when_open():
    assert day_in_range("?", None, None) is True
    assert day_in_range("?", "2026-06-01", None) is False
    assert day_in_range("?", None, "2026-06-10") is False
