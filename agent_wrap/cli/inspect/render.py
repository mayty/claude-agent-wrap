# This file has been edited with the assistance of an AI tool.
"""
Terminal rendering for the inspect command.

Four tables: the sidecar containers, the agent containers, everything else, and the
registered projects whose own image is already stale. ``Details`` — the third — is a table
rather than a block of ``Label: value`` lines so the whole report reads as one kind of
output, and its three concerns (logs, secrets, wrapper/host) are separated by dividers
rather than left to run together. The fourth closes the report because its empty state is
a green line standing in its place, and only a trailing section can be replaced that way.

Each renderer returns a renderable rather than printing, so ``run.py`` owns all output and
the whole report is assembled before anything reaches the terminal.

Colour carries one meaning throughout: green confirms something is ready, yellow flags
something the user may want to act on (a container not running, a stale image, a missing
base image), dim marks context that is merely informational, and plain text is the normal
case. A container with zero attached agents is deliberately *not* flagged — that is a
legitimate transient state during teardown, not a fault. A provider whose secrets are
not set is dim rather than yellow for the same reason: `agent run` prompts for them on
the next launch, so it is a state to know about and not a fault to go and fix.

Rows that have nothing to say are omitted rather than filled in: a project that declares
no Dockerfile gets no ``project image`` row at all, and a stale-image table with no rows
is not drawn empty. A lite report closes with one line naming what it skipped, instead of
marking each row the omission touched.
"""

import time
from datetime import timedelta
from typing import TYPE_CHECKING

from humanize import naturaltime
from rich.console import Group
from rich.text import Text

from agent_wrap.cli.inspect.constants import (
    AGENT_TABLE,
    DETAILS_TABLE,
    DETAILS_TITLE,
    LEGACY_DOCKERFILE_NOTE,
    LITE_NOTE,
    NO_STALE_IMAGES,
    NONE_CELL,
    NOT_MEASURED,
    PROJECT_IMAGE_LABEL,
    SIDECAR_TABLE,
    STALE_IMAGES_TABLE,
    UNKNOWN,
)
from agent_wrap.constants import (
    AUTOSTART_LOGS_ENV,
    DIVIDER,
    NO_HEALTHCHECK,
    RUNNING_STATUS,
    SKIP_SAFETY_CHECK_ENV,
)
from agent_wrap.domain.display.constants import Style
from agent_wrap.domain.display.models import RowItem
from agent_wrap.lib.path_tree import build_path_tree, expand_widest_chain, walk_path_tree

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.status.models import (
        AgentRow,
        AutostartRow,
        EnvironmentRow,
        InspectReport,
        ProjectImageRow,
        ProviderRow,
        SidecarRow,
        StaleImageRow,
        StorageRow,
        ViewerRow,
        WrapperRow,
    )
    from agent_wrap.lib.path_tree import PathTreeLine, PathTreeNode


class Cells:
    @staticmethod
    def port(port: int | None) -> str:
        return UNKNOWN if port is None else str(port)

    @staticmethod
    def status(status: str, exit_code: int | None) -> str:
        """Render a container's state, appending the exit code when it exited."""
        if status == "exited" and exit_code is not None:
            return f"exited ({exit_code})"
        return status or UNKNOWN

    @staticmethod
    def container_label(name: str, *, stale_image: bool) -> str:
        """Name the container, marking a sidecar running something other than the pin."""
        return f"{name} (stale image)" if stale_image else name

    @staticmethod
    def image(image: str) -> str:
        """
        Shorten a sidecar's image reference to the part that identifies the build.

        A digest-pinned reference is ~110 characters and would wrap the row. Drift from
        the pin is already flagged on the CONTAINER cell, so the name and tag left here
        are what say *which* build drifted.
        """
        return image.split("@", 1)[0].rsplit("/", 1)[-1] or UNKNOWN

    @staticmethod
    def stale_row(line: PathTreeLine[StaleImageRow]) -> RowItem:
        """
        Build one stale-images row from a walked tree line.

        A structural line is a directory with no image to rebuild, so its two remaining
        cells stay empty rather than carrying a subtotal nobody would act on.
        """
        row = line.node.row
        if row is None:
            return RowItem(cells=[line.label, "", ""], style=Style.DIM, prefix_len=line.prefix_len)
        return RowItem(
            cells=[line.label, row.image, row.reason],
            style=Style.BOLD_YELLOW,
            prefix_len=line.prefix_len,
        )

    @staticmethod
    def row_style(status: str) -> Style:
        """Flag anything not running; leave a healthy row unstyled."""
        return Style.NONE if status == RUNNING_STATUS else Style.BOLD_YELLOW

    @staticmethod
    def row(label: str, value: str, style: Style = Style.NONE) -> RowItem:
        return RowItem(cells=[label, value], style=style, prefix_len=0)


class Tables:
    """The two container tables, and the fleet-wide stale-image table under them."""

    @staticmethod
    def sidecars(
        rows: list[SidecarRow], queued: list[str], display: DisplayService
    ) -> list[RenderableType]:
        """
        Render the sidecar table, plus the footnotes about the shared sidecar lock.

        The queued-launch count belongs here because those launches are blocked waiting on
        one of this table's containers.
        """
        if not rows:
            return ["No sidecars are running.", *Tables.lock_footnotes([], queued)]

        body: list[RowItemOrDivider] = [
            RowItem(
                cells=[
                    Cells.container_label(row.name, stale_image=row.stale_image),
                    row.role,
                    Cells.image(row.image),
                    Cells.status(row.status, row.exit_code),
                    row.health or NO_HEALTHCHECK,
                    display.format_duration(row.uptime_sec),
                    Cells.port(row.port),
                    str(row.attached_agents),
                ],
                style=Cells.row_style(row.status),
                prefix_len=0,
            )
            for row in rows
        ]
        idle = [
            row.name for row in rows if row.status == RUNNING_STATUS and not row.attached_agents
        ]
        return [
            display.render_table(f"Sidecars ({len(rows)}):", SIDECAR_TABLE, body),
            *Tables.lock_footnotes(idle, queued),
        ]

    @staticmethod
    def lock_footnotes(idle: list[str], queued: list[str]) -> list[str]:
        """
        Note idle sidecars and queued launches under the sidecar table.

        Stated without a suggested action: teardown clears registrations before stopping
        the container, so an idle sidecar is a normal transient state.
        """
        lines: list[str] = []
        if idle:
            lines.append(f"  {len(idle)} sidecar(s) with no agents attached: {', '.join(idle)}")
        if queued:
            lines.append(f"  {len(queued)} launch(es) awaiting the sidecar lock")
        return lines

    @staticmethod
    def agents(rows: list[AgentRow], display: DisplayService) -> list[RenderableType]:
        """Render the agent table, or a one-line note when there are none."""
        if not rows:
            return ["No agents are running."]

        body: list[RowItemOrDivider] = [
            RowItem(
                cells=[
                    row.image or UNKNOWN,
                    row.cwd or UNKNOWN,
                    row.provider or NONE_CELL,
                    Cells.status(row.status, None),
                    display.format_duration(row.uptime_sec),
                ],
                style=Cells.row_style(row.status),
                prefix_len=0,
            )
            for row in rows
        ]
        return [display.render_table(f"Agents ({len(rows)}):", AGENT_TABLE, body)]

    @staticmethod
    def stale_images(
        rows: list[StaleImageRow] | None, display: DisplayService
    ) -> list[RenderableType]:
        """
        Render one row per registered project whose own image is already stale.

        The two empty cases are not interchangeable: None means the sweep never ran, while
        an empty list is the measured verdict that nothing is stale -- the one section
        whose empty state is good news, and so the one green line in the report. It stands
        in for the table and not for the gap above it, which is why the blank line belongs
        to the table rather than to the caller.

        ``PROJECT`` is chopped a segment at a time until the table fits, before ``IMAGE``
        and ``REASON`` give up characters -- chopping costs height, not information, and a
        truncated path identifies nothing. ``PROJECT`` never ellipsises.

        The title counts projects, not lines, so it agrees with ``--json`` however the
        tree comes out.
        """
        if rows is None:
            return []
        if not rows:
            return [Text(NO_STALE_IMAGES, style=Style.BOLD_GREEN)]

        placed = [row for row in rows if row.project]
        unplaceable = [row for row in rows if not row.project]
        root = build_path_tree([(row.project, row) for row in placed]) if placed else None

        # Chop the tree only while the tree is the thing that does not fit; the spec's
        # elidable columns absorb whatever is left over at render time.
        body, shared = display.fit_table(
            STALE_IMAGES_TABLE,
            lambda: Tables.stale_body(root, unplaceable),
            shrink=lambda: root is not None and expand_widest_chain(root),
        )
        return [
            "",
            display.render_table(f"Stale images ({len(rows)}):", STALE_IMAGES_TABLE, body, shared),
        ]

    @staticmethod
    def stale_body(
        root: PathTreeNode[StaleImageRow] | None, unplaceable: list[StaleImageRow]
    ) -> list[RowItemOrDivider]:
        """
        Lay the stale-image rows out under the tree, rebuildable as the tree is chopped.

        A row whose project is empty cannot be placed in the tree, and dropping it would
        leave the title counting a row nothing shows -- so it is listed flat underneath.
        """
        body: list[RowItemOrDivider] = []
        if root is not None:
            body.append(RowItem(cells=[root.name, "", ""], style=Style.DIM, prefix_len=0))
            body.extend(Cells.stale_row(line) for line in walk_path_tree(root))
        body.extend(
            RowItem(
                cells=[UNKNOWN, row.image, row.reason],
                style=Style.BOLD_YELLOW,
                prefix_len=0,
            )
            for row in unplaceable
        )
        return body


class Details:
    """
    The details table: three row groups covering everything that is not a container.

    Each group builds its own rows and knows nothing about the others; ``table`` joins
    them with dividers, so a group that renders nothing simply contributes no rows.
    """

    @staticmethod
    def logs_rows(
        viewer: ViewerRow,
        autostart: AutostartRow,
        storage: StorageRow,
        display: DisplayService,
    ) -> list[RowItem]:
        if viewer.running:
            detail = f"(pid {viewer.pid}" if viewer.pid is not None else "("
            if viewer.log_size is not None:
                detail += f", log {display.format_bytes(viewer.log_size)}"
            detail += ")"
            # "starting" is a distinct report, not a flavour of running: the process is up
            # but nothing is listening yet, so there is no connect line to hand over.
            if viewer.starting:
                state = f"starting  {detail}"
            else:
                state = f"running  {viewer.connect_line}  {detail}"
            viewer_row = Cells.row("logs viewer", state)
        else:
            viewer_row = Cells.row("logs viewer", "not running")

        # The stale count is an observation and never a prompt to run `agent cleanup`.
        # Staleness is decided by whether a registered path still has a logs directory,
        # which is only answerable from the host: run from inside an agent container,
        # where other projects' paths are not mounted, every entry looks stale. Acting on
        # that reading deletes live logs, so this states the number and leaves the
        # decision to someone who knows where they are.
        size = (
            NOT_MEASURED if storage.logs_bytes is None else display.format_bytes(storage.logs_bytes)
        )
        text = f"{size} · {storage.projects_registered} project(s) registered"
        if storage.projects_stale:
            text += f", {storage.projects_stale} with no logs directory"
        autostart_text, autostart_style = Details.logs_autostart(autostart)
        index_text, index_style = Details.logs_index(storage, display)
        return [
            viewer_row,
            Cells.row("logs viewer autostart", autostart_text, autostart_style),
            Cells.row("logs storage", text),
            Cells.row("request index", index_text, index_style),
        ]

    @staticmethod
    def logs_index(storage: StorageRow, display: DisplayService) -> tuple[str, Style]:
        """
        Report the index's size, what it holds, and how far behind the log files it is.

        Freshness is an age rather than a date: the question is whether ingest is keeping
        up. Deliberately silent about lag in `--lite` mode rather than reporting zero --
        not measured is not the same as up to date.
        """
        parts = [
            display.format_bytes(storage.index_bytes),
            f"{storage.index_sessions} session(s), {storage.index_requests} request(s)",
        ]
        if storage.index_last_ingested is None:
            parts.append("never ingested")
        else:
            age = time.time() - storage.index_last_ingested
            parts.append(f"last ingest {naturaltime(timedelta(seconds=age))}")
        if storage.index_behind:
            parts.append(f"{storage.index_behind} behind — run `agent reindex`")
            return " · ".join(parts), Style.BOLD_YELLOW
        return " · ".join(parts), Style.NONE

    @staticmethod
    def logs_autostart(autostart: AutostartRow) -> tuple[str, Style]:
        """
        Whether the next `agent run` would start the viewer, and what decides it.

        Inverted relative to `host network`: this is on by default, so "off" is the state
        worth noticing. Requested-but-ignored is flagged because a variable that is set
        and does nothing otherwise reads as the setting not working.
        """
        if autostart.effective:
            return "on", Style.NONE
        if autostart.requested is False:
            return f"OFF ({AUTOSTART_LOGS_ENV})", Style.NONE
        reason = f"{autostart.declining_provider} does not use it"
        if autostart.requested:
            return f"requested but IGNORED ({reason})", Style.BOLD_YELLOW
        return f"OFF ({reason})", Style.NONE

    @staticmethod
    def secrets_rows(rows: list[ProviderRow]) -> list[RowItem]:
        """
        One row per known provider/sidecar and its secret readiness.

        The required key names are deliberately left out -- `agent secrets check` prints
        them.
        """
        if not rows:
            return [Cells.row("providers", "none discovered")]
        out: list[RowItem] = []
        for row in rows:
            label = f"{row.name} (default)" if row.is_default else row.name
            if row.secrets_ok:
                out.append(Cells.row(label, "Secrets OK", Style.BOLD_GREEN))
            else:
                out.append(Cells.row(label, "Secrets NOT SET", Style.DIM))
        return out

    @staticmethod
    def wrapper_rows(
        wrapper: WrapperRow, environment: EnvironmentRow, project: ProjectImageRow | None
    ) -> list[RowItem]:
        """
        Report the installed revision, then the host facts behind most surprises.

        The project image sits directly under the base image it inherits from, since the
        two are read together.
        """
        host_net, host_net_style = Details.host_network(environment)
        return [
            Cells.row("wrapper", Details.revision(wrapper)),
            Details.interpreter_row(wrapper),
            Details.base_image_row(environment),
            *Details.project_image_rows(project, environment),
            Details.network_row(environment),
            Cells.row("host network", host_net, host_net_style),
            # Two states, and neither is an anomaly: an unset variable is the guard doing
            # its job, and a set one is a choice its owner made. Nothing can ignore it, so
            # there is no requested-but-IGNORED reading to flag the way the row above has.
            Cells.row(
                "directory guard",
                "on" if environment.safety_check_enabled else f"OFF ({SKIP_SAFETY_CHECK_ENV})",
            ),
            Cells.row("day boundary", Details.day_boundary(environment)),
        ]

    @staticmethod
    def revision(wrapper: WrapperRow) -> str:
        """Describe the wrapper's local git identity, or note that there is none."""
        if not wrapper.branch and not wrapper.commit:
            return "not a git checkout"
        detail = wrapper.branch
        if wrapper.commit:
            detail += f" @ {wrapper.commit}"
        if wrapper.describe:
            detail += f" ({wrapper.describe})"
        if wrapper.dirty:
            detail += " [dirty]"
        return detail

    @staticmethod
    def interpreter_row(wrapper: WrapperRow) -> RowItem:
        """
        Report the provisioned CPython, flagged when it has fallen behind the pin.

        A drifted interpreter still runs, so it is a warning rather than an error --
        but nothing else in the report would reveal it.

        Stale dependencies are the same shape of drift and only show up after a manual
        ``git pull``, since ``agent update`` re-runs the bootstrap itself. The pin is
        reported first when both have moved: one bootstrap run fixes both.
        """
        running = wrapper.python_version
        pinned = wrapper.python_pinned
        if running is None:
            return Cells.row("interpreter", "not provisioned", Style.BOLD_YELLOW)
        if pinned and pinned != running:
            return Cells.row(
                "interpreter",
                f"{running} (pinned {pinned}) -- run bin/agent-bootstrap",
                Style.BOLD_YELLOW,
            )
        if wrapper.deps_current is False:
            return Cells.row(
                "interpreter",
                f"{running} (dependencies stale) -- run bin/agent-bootstrap",
                Style.BOLD_YELLOW,
            )
        return Cells.row("interpreter", running)

    @staticmethod
    def base_image_row(environment: EnvironmentRow) -> RowItem:
        """
        Whether the base image exists, which Claude Code it carries, and if that is stale.

        Plain when present and current, *including* when the registry was not consulted --
        ``--lite`` leaves the latest version unknown, and an unknown latest must never
        look like an update.

        Absence is not an instruction: ``agent run`` builds a missing image itself.
        """
        if not environment.base_image_present:
            return Cells.row(
                "base image",
                f"{environment.base_image} MISSING (built on the next `agent run`)",
                Style.BOLD_YELLOW,
            )
        state = f"{environment.base_image} present"
        if environment.base_image_version:
            state += f" (Claude Code v{environment.base_image_version})"
        if environment.base_image_stale_reason:
            state += (
                f" -- STALE, rebuilt on the next `agent run`: {environment.base_image_stale_reason}"
            )
            return Cells.row("base image", state, Style.BOLD_YELLOW)
        if environment.claude_update_available and environment.latest_claude_version is not None:
            state += f" → v{environment.latest_claude_version} available"
            return Cells.row("base image", state, Style.BOLD_YELLOW)
        return Cells.row("base image", state)

    @staticmethod
    def project_image_rows(
        project: ProjectImageRow | None, environment: EnvironmentRow
    ) -> list[RowItem]:
        """
        Describe the image this project's Dockerfile declares, or nothing when it declares
        none.

        Absent rather than reported as empty, which would be noise in every project that
        never customized anything.

        The available version is read off *environment*: there is one registry answer per
        report, and naming it keeps the two image rows directly comparable.
        """
        if project is None:
            return []
        if not project.present:
            return [
                Cells.row(
                    PROJECT_IMAGE_LABEL,
                    f"{project.image} MISSING (built on the next `agent run`)",
                    Style.BOLD_YELLOW,
                )
            ]
        state = f"{project.image} present"
        if project.claude_version:
            state += f" (Claude Code v{project.claude_version})"
        style = Style.NONE
        if project.stale_reason:
            state += f" -- STALE, rebuilt on the next `agent run`: {project.stale_reason}"
            style = Style.BOLD_YELLOW
        if project.claude_update_available and environment.latest_claude_version is not None:
            state += f" → v{environment.latest_claude_version} available"
            style = Style.BOLD_YELLOW
        if project.is_legacy:
            state += LEGACY_DOCKERFILE_NOTE
            style = Style.BOLD_YELLOW
        return [Cells.row(PROJECT_IMAGE_LABEL, state, style)]

    @staticmethod
    def network_row(environment: EnvironmentRow) -> RowItem:
        """
        Whether the shared sidecar network exists.

        Absent is stated without yellow: docker creates it on the next launch.
        """
        if environment.network_present:
            return Cells.row("network", f"{environment.network_name} present")
        return Cells.row("network", f"{environment.network_name} absent (created on next launch)")

    @staticmethod
    def host_network(environment: EnvironmentRow) -> tuple[str, Style]:
        """
        Whether AGENT_USE_HOST_NETWORK is set and whether it will actually apply.

        Requested-but-ignored is flagged because it is silently dropped off WSL, which
        otherwise reads as the setting simply not working.
        """
        if not environment.host_network_requested:
            return "off", Style.NONE
        if environment.host_network_effective:
            return "ON", Style.NONE
        return "requested but IGNORED (only honored on WSL)", Style.BOLD_YELLOW

    @staticmethod
    def day_boundary(environment: EnvironmentRow) -> str:
        """
        Describe the resolved stats day boundary, noting an explicit override.

        Stated against UTC midnight, the only reading that makes a bare number meaningful.
        The sign is dropped at zero, where "+0h" reads as a formatting artefact.
        """
        hours = environment.day_start_hours
        day = f"{hours:+d}h UTC" if hours else "0h UTC"
        if environment.day_start_overridden:
            day += " (AGENT_DAY_START_UTC)"
        elif environment.day_start_timezone:
            day += f" (AGENT_TIMEZONE={environment.day_start_timezone})"
        return day

    @staticmethod
    def table(report: InspectReport, display: DisplayService) -> Group:
        groups = [
            Details.logs_rows(report.viewer, report.logs_autostart, report.storage, display),
            Details.secrets_rows(report.providers),
            Details.wrapper_rows(report.wrapper, report.environment, report.project),
        ]
        body: list[RowItemOrDivider] = []
        for group in groups:
            if not group:
                continue
            if body:
                body.append(DIVIDER)
            body.extend(group)

        return display.render_table(DETAILS_TITLE, DETAILS_TABLE, body)


def render(report: InspectReport, display: DisplayService) -> Group:
    parts: list[RenderableType] = []
    if not report.docker.available:
        parts.append(report.docker.error)
        parts.append("")
    else:
        parts.extend(Tables.sidecars(report.sidecars, report.queued_launches, display))
        parts.append("")
        parts.extend(Tables.agents(report.agents, display))
        parts.append("")

    parts.append(Details.table(report, display))
    if report.lite:
        # One closing line rather than a marker on each affected row: the skipped steps are
        # a property of the run, and naming them once keeps the tables reading the same in
        # both modes.
        parts.append(LITE_NOTE)

    # Last, and never in lite mode, so it can never collide with the note above.
    parts.extend(Tables.stale_images(report.stale_images, display))
    return Group(*parts)
