# This file has been created with the assistance of an AI tool.
"""
Custom ``click.ParamType`` converters shared by the agent-wrap subcommands.

Per-value validation belongs here; click renders a failure as
``Invalid value for '-f' / '--from': <message>`` and exits 2. Cross-field rules a
converter cannot see (``--from``/``--until``/``--days`` interacting, say) stay in the
command body instead.

Both types accept their own output as input, and return it untouched, because click
runs ``convert()`` over default values as well as over what the user typed.
"""

import re
from datetime import date, datetime, timedelta
from typing import override

import click

from agent_wrap.cli.stats.constants import RELATIVE_DATE_RE
from agent_wrap.constants import DAY_START_HOURS
from agent_wrap.containers import services
from agent_wrap.lib.daytime import get_day


class DateSpec(click.ParamType[date, "str | date"]):
    """
    A calendar day, given as an absolute ISO date or a relative ``-Nd`` offset.

    The relative form counts back from *today* as the configured day boundary sees it
    (``AGENT_DAY_START_UTC`` / ``AGENT_TIMEZONE``), not from UTC midnight — so
    ``--from -14d`` selects the same lower bound the report's day buckets use.
    """

    name = "date"

    @override
    def convert(
        self, value: str | date, param: click.Parameter | None, ctx: click.Context | None
    ) -> date:
        if isinstance(value, date):
            return value
        rel = RELATIVE_DATE_RE.match(value)
        if rel is not None:
            today = get_day(services.stats_service.now_utc(), DAY_START_HOURS)
            return today - timedelta(days=int(rel.group(1)))
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
        except ValueError:
            self.fail(f"expects YYYY-MM-DD or -Nd (e.g. -14d), got '{value}'", param, ctx)


class Regex(click.ParamType[re.Pattern[str], "str | re.Pattern[str]"]):
    """A regular expression, compiled at parse time so a bad pattern fails as a usage error."""

    name = "regex"

    @override
    def convert(
        self,
        value: str | re.Pattern[str],
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> re.Pattern[str]:
        if isinstance(value, re.Pattern):
            return value
        try:
            return re.compile(value)
        except re.error as exc:
            self.fail(f"invalid regex pattern: {exc}", param, ctx)
