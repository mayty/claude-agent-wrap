# This file has been created with the assistance of an AI tool.
"""
Terminal rendering for the inspect command.

Three tables: the sidecar containers, the agent containers, and everything else. That
last one — ``Details`` — is a table rather than a block of ``Label: value`` lines so the
whole report reads as one kind of output, and its three concerns (logs, secrets,
wrapper/host) are separated by dividers rather than left to run together.

Each renderer returns lines rather than printing, so ``run.py`` owns all output and the
whole report can be assembled before anything reaches the terminal.

Colour carries one meaning throughout: green confirms something is ready, yellow flags
something the user may want to act on (a container not running, a stale image, a missing
base image), dim marks context that is merely informational, and plain text is the normal
case. A container with zero attached agents is deliberately *not* flagged — that is a
legitimate transient state during teardown, not a fault. A provider whose secrets are
not set is dim rather than yellow for the same reason: `agent run` prompts for them on
the next launch, so it is a state to know about and not a fault to go and fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli.inspect.constants import (
    AGENT_ALIGNS,
    AGENT_HEADERS,
    DETAILS_ALIGNS,
    DETAILS_HEADERS,
    DETAILS_TITLE,
    NONE_CELL,
    SIDECAR_ALIGNS,
    SIDECAR_HEADERS,
    UNKNOWN,
)
from agent_wrap.constants import DIVIDER, NO_HEALTHCHECK, RUNNING_STATUS
from agent_wrap.domain.display.constants import Ansi
from agent_wrap.domain.display.models import RowItem

if TYPE_CHECKING:
    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.status.models import (
        AgentRow,
        EnvironmentRow,
        InspectReport,
        ProviderRow,
        SidecarRow,
        StorageRow,
        ViewerRow,
        WrapperRow,
    )

#: Divider sentinel understood by ``DisplayService.render_table``.


class Cells:
    """Value-to-cell conversions shared by the tables."""

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

        Sidecars are pinned by digest, so the reference docker reports is around 110
        characters — long enough to wrap the row on any normal terminal. The digest is
        dropped because nobody reads a sha256 off a table, and the registry path with
        it: whether a build came from ghcr.io or Docker Hub is a property of the pin,
        not of this container. Drift from the pin is already flagged on the CONTAINER
        cell, so the name and tag left here are what tells you *which* build drifted.
        """
        return image.split("@", 1)[0].rsplit("/", 1)[-1] or UNKNOWN

    @staticmethod
    def row_style(status: str) -> Ansi:
        """Flag anything not running; leave a healthy row unstyled."""
        return Ansi.NONE if status == RUNNING_STATUS else Ansi.BOLD_YELLOW

    @staticmethod
    def row(label: str, value: str, style: Ansi = Ansi.NONE) -> RowItem:
        """Build one two-column details row."""
        return RowItem(cells=[label, value], style=style, prefix_len=0)


class Tables:
    """The two container tables."""

    @staticmethod
    def sidecars(rows: list[SidecarRow], queued: list[str], display: DisplayService) -> list[str]:
        """
        Render the sidecar table, plus the footnotes about the shared sidecar lock.

        The queued-launch count lives here rather than with the details below, because it
        is a fact about this table's containers: those launches are blocked waiting to
        start or attach to one of them.
        """
        if not rows:
            lines = ["No sidecars are running."]
            return lines + Tables.lock_footnotes([], queued)

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
        headers = list(SIDECAR_HEADERS)
        shared = display.compute_shared_widths([(headers, body, 1)], len(headers) - 1)
        lines = display.render_table(
            f"Sidecars ({len(rows)}):", headers, list(SIDECAR_ALIGNS), body, 1, shared
        )
        idle = [
            row.name for row in rows if row.status == RUNNING_STATUS and not row.attached_agents
        ]
        return lines + Tables.lock_footnotes(idle, queued)

    @staticmethod
    def lock_footnotes(idle: list[str], queued: list[str]) -> list[str]:
        """
        Note idle sidecars and queued launches under the sidecar table.

        Both are stated as facts, with no suggested action: teardown clears registrations
        before it stops the container, so an idle sidecar is a normal transient state,
        and a queued launch resolves itself once the lock is free.
        """
        lines: list[str] = []
        if idle:
            lines.append(f"  {len(idle)} sidecar(s) with no agents attached: {', '.join(idle)}")
        if queued:
            lines.append(f"  {len(queued)} launch(es) awaiting the sidecar lock")
        return lines

    @staticmethod
    def agents(rows: list[AgentRow], display: DisplayService) -> list[str]:
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
        headers = list(AGENT_HEADERS)
        shared = display.compute_shared_widths([(headers, body, 1)], len(headers) - 1)
        return display.render_table(
            f"Agents ({len(rows)}):", headers, list(AGENT_ALIGNS), body, 1, shared
        )


class Details:
    """
    The details table: three row groups covering everything that is not a container.

    Each group builds its own rows and knows nothing about the others; ``table`` joins
    them with dividers, so a group that renders nothing simply contributes no rows.
    """

    @staticmethod
    def logs_rows(viewer: ViewerRow, storage: StorageRow, display: DisplayService) -> list[RowItem]:
        """Report the logs viewer's liveness and the on-disk footprint of what it serves."""
        if viewer.running:
            detail = f"(pid {viewer.pid}" if viewer.pid is not None else "("
            if viewer.log_size is not None:
                detail += f", log {display.format_bytes(viewer.log_size)}"
            detail += ")"
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
        text = (
            f"{display.format_bytes(storage.logs_bytes)} · "
            f"{storage.projects_registered} project(s) registered"
        )
        if storage.projects_stale:
            text += f", {storage.projects_stale} with no logs directory"
        return [viewer_row, Cells.row("logs storage", text)]

    @staticmethod
    def secrets_rows(rows: list[ProviderRow]) -> list[RowItem]:
        """
        One row per known provider/sidecar and its secret readiness.

        Readiness is stated as one of two states and nothing more. The required key
        names are deliberately left out: `agent secrets check` prints them, and here
        they only pad the row with detail that does not change what the reader does.
        """
        if not rows:
            return [Cells.row("providers", "none discovered")]
        out: list[RowItem] = []
        for row in rows:
            label = f"{row.name} (default)" if row.is_default else row.name
            if row.secrets_ok:
                out.append(Cells.row(label, "Secrets OK", Ansi.BOLD_GREEN))
            else:
                out.append(Cells.row(label, "Secrets NOT SET", Ansi.DIM))
        return out

    @staticmethod
    def wrapper_rows(wrapper: WrapperRow, environment: EnvironmentRow) -> list[RowItem]:
        """Report the installed revision, then the host facts behind most surprises."""
        host_net, host_net_style = Details.host_network(environment)
        return [
            Cells.row("wrapper", Details.revision(wrapper)),
            *Details.image_and_network_rows(environment),
            Cells.row("host network", host_net, host_net_style),
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
    def image_and_network_rows(environment: EnvironmentRow) -> list[RowItem]:
        """
        Whether the base image and the shared network exist.

        Both are yellow when absent, but only the image is actionable: docker creates the
        network on the next launch, while a missing base image needs a rebuild.
        """
        if environment.base_image_present:
            image_row = Cells.row("base image", f"{environment.base_image} present")
        else:
            image_row = Cells.row(
                "base image",
                f"{environment.base_image} MISSING (run `agent rebuild --full`)",
                Ansi.BOLD_YELLOW,
            )
        if environment.network_present:
            network_row = Cells.row("network", f"{environment.network_name} present")
        else:
            network_row = Cells.row(
                "network", f"{environment.network_name} absent (created on next launch)"
            )
        return [image_row, network_row]

    @staticmethod
    def host_network(environment: EnvironmentRow) -> tuple[str, Ansi]:
        """
        Whether AGENT_USE_HOST_NETWORK is set and whether it will actually apply.

        Requested-but-ignored is flagged because it is silently dropped off WSL, which
        otherwise reads as the setting simply not working.
        """
        if not environment.host_network_requested:
            return "off", Ansi.NONE
        if environment.host_network_effective:
            return "ON", Ansi.NONE
        return "requested but IGNORED (only honored on WSL)", Ansi.BOLD_YELLOW

    @staticmethod
    def day_boundary(environment: EnvironmentRow) -> str:
        """
        Describe the resolved stats day boundary, noting an explicit override.

        The offset is stated against UTC midnight, which is the only reading that makes
        a bare number meaningful. The sign is dropped at zero, where there is no
        direction to signal and "+0h" reads as a formatting artefact.
        """
        hours = environment.day_start_hours
        day = f"{hours:+d}h UTC" if hours else "0h UTC"
        if environment.day_start_overridden:
            day += " (AGENT_DAY_START_UTC)"
        return day

    @staticmethod
    def table(report: InspectReport, display: DisplayService) -> list[str]:
        """Render the three groups as one table, dividing group from group."""
        groups = [
            Details.logs_rows(report.viewer, report.storage, display),
            Details.secrets_rows(report.providers),
            Details.wrapper_rows(report.wrapper, report.environment),
        ]
        body: list[RowItemOrDivider] = []
        for group in groups:
            if not group:
                continue
            if body:
                body.append(DIVIDER)
            body.extend(group)

        headers = list(DETAILS_HEADERS)
        shared = display.compute_shared_widths([(headers, body, 1)], len(headers) - 1)
        return display.render_table(DETAILS_TITLE, headers, list(DETAILS_ALIGNS), body, 1, shared)


def render(report: InspectReport, display: DisplayService) -> list[str]:
    """Render the whole report as terminal lines, containers first."""
    lines: list[str] = []
    if not report.docker.available:
        lines.append(report.docker.error)
        lines.append("")
    else:
        lines.extend(Tables.sidecars(report.sidecars, report.queued_launches, display))
        lines.append("")
        lines.extend(Tables.agents(report.agents, display))
        lines.append("")

    lines.extend(Details.table(report, display))
    lines.extend(f"warning: {text}" for text in report.warnings)
    return lines
